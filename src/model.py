import torch
import torch.nn as nn
import torch.nn.functional as F
from gcn_layer import NR_GraphAttention
from tabulate import tabulate
import logging
from torch_scatter import scatter_mean


class Encoder_Model(nn.Module):
    def __init__(self, node_hidden, rel_hidden, triple_size, node_size, new_node_size, rel_size, device,
                 adj_matrix, r_index, r_val, rel_matrix, ent_matrix, alpha, beta, new_ent_nei,
                 dropout_rate=0.0, ind_dropout_rate=0.0, gamma=3, lr=0.005, depth=2):
        # 1000 kr dene se run ho jata hain chhote dataset par  
        super(Encoder_Model, self).__init__()
        self.node_hidden = node_hidden
        self.node_size = node_size
        self.rel_size = rel_size
        self.triple_size = triple_size
        self.depth = depth
        self.device = device
        self.dropout = nn.Dropout(dropout_rate)
        self.ind_dropout = nn.Dropout(ind_dropout_rate)
        self.gamma = gamma
        self.lr = lr
        self.adj_list = adj_matrix.to(device)
        self.r_index = r_index.to(device)
        self.r_val = r_val.to(device)
        self.rel_adj = rel_matrix.to(device)
        self.ent_adj = ent_matrix.to(device)
        self.ind_loss = nn.MSELoss(reduction='sum')
        self.alpha = alpha
        self.new_node_size = new_node_size
        self.beta = beta
        self.new_ent_nei = torch.from_numpy(new_ent_nei).long().to(device)

        self.ent_embedding = nn.Embedding(self.node_size, node_hidden)
        self.rel_embedding = nn.Embedding(self.rel_size, rel_hidden)
        torch.nn.init.xavier_uniform_(self.ent_embedding.weight)
        torch.nn.init.xavier_uniform_(self.rel_embedding.weight)

        self.e_encoder = NR_GraphAttention(node_size=self.new_node_size,
                                           rel_size=self.rel_size,
                                           triple_size=self.triple_size,
                                           node_dim=self.node_hidden,
                                           depth=self.depth,
                                           use_bias=True
                                           )
        self.r_encoder = NR_GraphAttention(node_size=self.new_node_size,
                                           rel_size=self.rel_size,
                                           triple_size=self.triple_size,
                                           node_dim=self.node_hidden,
                                           depth=self.depth,
                                           use_bias=True
                                           )
    def getEmbeddings(self,train_paris):
        out_feature = self.gcn_forward()
        l, r = train_paris[:, 0], train_paris[:, 1]
        return out_feature[l]
    
    def getEmbeddings2(self,train_paris):
        out_feature = self.gcn_forward()
        l, r = train_paris[:, 0], train_paris[:, 1]
        return out_feature[r]

    def corrupt_with_noise(self, embeddings, std_dev=0.01):
        noise = torch.randn_like(embeddings) * std_dev
        return embeddings + noise

    def corrupt_with_dropout(self, embeddings, dropout_rate=0.1):
        dropout_mask = torch.rand(embeddings.shape) < (1 - dropout_rate)
        return embeddings * dropout_mask.float()

    def corrupt_by_shuffling_features(self, embeddings):
        idx = torch.randperm(embeddings.size(1))
        return embeddings[:, idx]

    def corrupt_with_negative_sampling(self, embeddings, negative_sample_indices):
        return embeddings[negative_sample_indices]
    

    def ensemble_corruption(self, embeddings, negative_sample_indices=None, corruption_config=None):
        if corruption_config is None:
            corruption_config = {
                'noise': {'enabled': True, 'std_dev': 0.01},
                'dropout': {'enabled': True, 'rate': 0.1},
                'shuffle': {'enabled': True},
                'negative_sampling': {'enabled': False, 'indices': negative_sample_indices}
            }
        
        corrupted_embeddings = embeddings.clone()
        
        if corruption_config['noise']['enabled']:
            corrupted_embeddings = self.corrupt_with_noise(corrupted_embeddings, std_dev=corruption_config['noise']['std_dev'])
        
        if corruption_config['dropout']['enabled']:
            corrupted_embeddings = self.corrupt_with_dropout(corrupted_embeddings, dropout_rate=corruption_config['dropout']['rate'])
        
        if corruption_config['shuffle']['enabled']:
            corrupted_embeddings = self.corrupt_by_shuffling_features(corrupted_embeddings)
        
        if corruption_config['negative_sampling']['enabled'] and negative_sample_indices is not None:
            corrupted_embeddings = self.corrupt_with_negative_sampling(corrupted_embeddings, negative_sample_indices=corruption_config['negative_sampling']['indices'])
        
        return corrupted_embeddings
    
    def contrastive_loss(self, z_i, z_j,temperature=0.07):
        z_i = F.normalize(z_i, p=2, dim=1)
        z_j = F.normalize(z_j, p=2, dim=1)

        # Calculate the similarity matrix
        N = 2 * z_i.shape[0]  # Total number of embeddings
        z = torch.cat((z_i, z_j), dim=0)
        sim_matrix = torch.exp(torch.mm(z, z.t().contiguous()) / temperature)

        # Remove the similarity of embeddings to themselves
        sim_matrix = sim_matrix - torch.eye(N, device=self.device) * 1e12

        # Create positive mask
        pos_mask = torch.cat((torch.arange(z_i.shape[0]), torch.arange(z_i.shape[0])), dim=0)
        pos_mask = pos_mask.unsqueeze(0).repeat(N, 1).to(self.device)
        pos_mask = torch.eq(pos_mask, pos_mask.t()).float()

        # Compute loss
        pos_sim = sim_matrix * pos_mask
        pos_sim_sum = pos_sim.sum(dim=1)
        log_prob = torch.log(pos_sim_sum / sim_matrix.sum(dim=1))
        loss = -log_prob.mean()

        return loss

    def avg(self, adj, emb, size: int):
        adj = torch.sparse_coo_tensor(indices=adj, values=torch.ones_like(adj[0, :], dtype=torch.float),
                                      size=[self.node_size, size])
        adj = torch.sparse.softmax(adj, dim=1)
        return torch.sparse.mm(adj, emb)

    def gcn_forward(self):
        # [Ne x Ne] · [Ne x dim] = [Ne x dim]
        ent_feature = self.avg(self.ent_adj, self.ent_embedding.weight, self.node_size)
        # [Ne x Nr] · [Nr x dim] = [Ne x dim]
        rel_feature = self.avg(self.rel_adj, self.rel_embedding.weight, self.rel_size)

        opt = [self.rel_embedding.weight, self.adj_list, self.r_index, self.r_val]
        out_feature = torch.cat([self.e_encoder([ent_feature] + opt), self.r_encoder([rel_feature] + opt)], dim=-1)
        out_feature = self.dropout(out_feature)

        return out_feature

    def forward(self, train_paris:torch.Tensor, credible_pairs:torch.Tensor):
        out_feature = self.gcn_forward()
        l, r = train_paris[:, 0].long(), train_paris[:, 1].long()
        true_context_l = out_feature[l]
        true_context_r = out_feature[r]
        false_context_l = self.ensemble_corruption(out_feature[l])
        false_context_r = self.ensemble_corruption(out_feature[r])
        true_context = torch.cat([true_context_l, true_context_r], dim=0)
        false_context = torch.cat([false_context_l, false_context_r], dim=0)
        loss1 = self.align_loss(train_paris, out_feature)
        closs = self.contrastive_loss(true_context, false_context)
        if credible_pairs != None:  # finetune
            loss2 = self.inductive_loss(self.trainable_new_ent_embedding.weight, self.new_ent_nei)
            # loss3 = self.loss_no_neg_samples(credible_pairs, out_feature)
            return loss1  + self.alpha * (train_paris.shape[0] / self.node_size) * loss2 + closs
        else:  # retrain
            loss2 = self.inductive_loss(self.ent_embedding.weight, self.adj_list)
            return loss1 + self.alpha * (train_paris.shape[0] / self.node_size) * loss2 + closs

    def inductive_loss(self, ent_embed, edge_index):
        neighs = self.ent_embedding.weight[edge_index[1].long()]
        neighs = self.ind_dropout(neighs)
        out = scatter_mean(src=neighs, dim=0, index=edge_index[0].long(), dim_size=self.node_size)
        loss = self.ind_loss(out, ent_embed)
        return loss

    def align_loss(self, pairs, emb):
        def squared_dist(A, B):
            row_norms_A = torch.sum(torch.square(A), dim=1)
            row_norms_A = torch.reshape(row_norms_A, [-1, 1])
            row_norms_B = torch.sum(torch.square(B), dim=1)
            row_norms_B = torch.reshape(row_norms_B, [1, -1])
            return row_norms_A + row_norms_B - 2 * torch.matmul(A, B.t())

        l, r = pairs[:, 0].long(), pairs[:, 1].long()
        l_emb, r_emb = emb[l], emb[r]

        pos_dis = torch.sum(torch.square(l_emb - r_emb), dim=-1, keepdim=True)
        l_neg_dis = squared_dist(l_emb, emb)
        r_neg_dis = squared_dist(r_emb, emb)

        del l_emb, r_emb

        l_loss = pos_dis - l_neg_dis + self.gamma
        l_loss = l_loss * (1 - F.one_hot(l, num_classes=self.node_size) - F.one_hot(r, num_classes=self.node_size))
        r_loss = pos_dis - r_neg_dis + self.gamma
        r_loss = r_loss * (1 - F.one_hot(l, num_classes=self.node_size) - F.one_hot(r, num_classes=self.node_size))

        del r_neg_dis, l_neg_dis

        r_loss = (r_loss - torch.mean(r_loss, dim=-1, keepdim=True).detach()) / torch.std(r_loss, dim=-1, unbiased=False, keepdim=True).detach()
        l_loss = (l_loss - torch.mean(l_loss, dim=-1, keepdim=True).detach()) / torch.std(l_loss, dim=-1, unbiased=False, keepdim=True).detach()

        lamb, tau = 30, 10
        l_loss = torch.logsumexp(lamb * l_loss + tau, dim=-1)
        r_loss = torch.logsumexp(lamb * r_loss + tau, dim=-1)
        return torch.mean(l_loss + r_loss)

    def loss_no_neg_samples(self, pairs, emb):
        if len(pairs) == 0:
            return 0.0

        l, r = pairs[:, 0].long(), pairs[:, 1].long()
        l_emb, r_emb = emb[l], emb[r]
        loss = torch.sum(torch.square(l_emb - r_emb), dim=-1)
        loss = torch.sum(loss)

        return loss

    def get_embeddings(self, index_a, index_b):
        # forward
        out_feature = self.gcn_forward()
        out_feature = out_feature.cpu()

        # get embeddings
        index_a = torch.Tensor(index_a).long()
        index_b = torch.Tensor(index_b).long()
        Lvec = out_feature[index_a]
        Rvec = out_feature[index_b]
        Lvec = Lvec / (torch.linalg.norm(Lvec, dim=-1, keepdim=True) + 1e-5)
        Rvec = Rvec / (torch.linalg.norm(Rvec, dim=-1, keepdim=True) + 1e-5)

        return Lvec, Rvec

    def print_all_model_parameters(self):
        logging.info('\n------------Model Parameters--------------')
        info = []
        head = ["Name", "Element Nums", "Element Bytes", "Total Size (MiB)", "requires_grad"]
        total_size = 0
        total_element_nums = 0
        for name, param in self.named_parameters():
            info.append((name,
                         param.nelement(),
                         param.element_size(),
                         round((param.element_size()*param.nelement())/2**20, 3),
                         param.requires_grad)
                        )
            total_size += (param.element_size()*param.nelement())/2**20
            total_element_nums += param.nelement()
        logging.info(tabulate(info, headers=head, tablefmt="grid"))
        logging.info(f'Total # parameters = {total_element_nums}')
        logging.info(f'Total # size = {round(total_size, 3)} (MiB)')
        logging.info('--------------------------------------------')
        logging.info('')

    def generate_new_features(self, new_entity_neighs):
        new_entity_neighs = new_entity_neighs.to(self.device)
        neighs_embedding = self.ent_embedding.weight[new_entity_neighs[1].long()]
        out = scatter_mean(src=neighs_embedding, dim=0, index=new_entity_neighs[0].long(), dim_size=self.new_node_size)

        new_ent_embedding = nn.Embedding(self.new_node_size, self.node_hidden).to(self.device)
        torch.nn.init.xavier_uniform_(new_ent_embedding.weight)
        new_ent_embedding.weight.requires_grad_(False)
        new_ent_embedding.weight[:self.node_size] = self.ent_embedding.weight
        new_ent_embedding.weight[self.node_size:] = out[self.node_size:]

        self.ent_embedding = nn.Embedding.from_pretrained(new_ent_embedding.weight, freeze=False)
        self.old_node_size = self.node_size
        self.trainable_new_ent_embedding = nn.Embedding.from_pretrained(new_ent_embedding.weight, freeze=False)

        self.node_size = self.new_node_size
