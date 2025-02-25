from dataclasses import dataclass
import numpy as np
import torch, math
import random
import torch.nn as nn
from torch.distributions.categorical import Categorical
from torch.distributions.normal import Normal
import torch.nn.functional as F
import copy
from copy import deepcopy
from typing import List, Dict, Optional
import torch.distributions as dist

class DiagonalLayer(nn.Module):
    def __init__(self, size):
        super(DiagonalLayer, self).__init__()
        self.scale = nn.Parameter(torch.ones(size))
        self.bias = nn.Parameter(torch.zeros(size))

    def forward(self, x):
        return x * self.scale + self.bias

class CReLU(nn.Module):
    def __init__(self):
        super().__init__()
    def forward(self, x):
        return torch.cat((x, F.relu(x)), dim=1)

class ConvCReLU(nn.Module):

    def __init__(self, inplace=False):
        super(ConvCReLU, self).__init__()

    def forward(self, x):
        # Concatenate along the channel dimension.
        channel_dim = 1
        x = torch.cat((x,-x), channel_dim)
        return F.relu(x)

def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False  #deterministic results on GPUs
    
def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer

class PPO_minatar_Agent(nn.Module):
    def __init__(self, envs, seed=None, hidden_size=256):
        set_seed(seed)
        super().__init__()
        self.critic = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), hidden_size)),
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, hidden_size)),#can be changed to 256
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, 1), std=1.0),
        )
        self.actor = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), hidden_size)),
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, hidden_size)),
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, envs.single_action_space.n), std=0.01),
        )
        self.init_critic_params = self.get_flat_params(self.critic).detach()
        self.init_actor_params = self.get_flat_params(self.actor).detach()

    def get_value(self, x):
        return self.critic(x)
    
    def set_flat_params(self):
        self.init_critic_params = self.get_flat_params(self.critic).detach()
        self.init_actor_params = self.get_flat_params(self.actor).detach()

    def get_flat_params(self, module):
        return torch.cat([p.flatten() for p in module.parameters()])
    
    def compute_l2_loss(self, device= torch.device("cuda" if torch.cuda.is_available() else "cpu")):
        curr_params = self.get_flat_params(self.critic)
        l2_loss = 0.5 * ((curr_params.to(device) - self.init_critic_params.to(device)) ** 2).sum()#.mean()
        return l2_loss
    
    def get_action_and_value(self, x, action=None):
        logits = self.actor(x)
        probs = Categorical(logits=logits)
        if action is None:
            action = probs.sample()
        return action, probs.log_prob(action), probs.entropy(), self.critic(x)

class RandomAgent(nn.Module):
    def __init__(self, envs):
        import gym
        self.envs = envs
        if isinstance(envs.single_action_space, gym.spaces.Discrete):
            self.action_dim = envs.single_action_space.n
        elif isinstance(envs.single_action_space, gym.spaces.Box):
            self.action_dim = envs.single_action_space.shape[0]
    def parameters(self):
        return []
    def get_action_and_value(self, x, action=None):
        #randomly sample 1 action from action space
        action = torch.tensor([self.envs.single_action_space.sample() for _ in range(len(x))])
        log_prob = torch.zeros(len(action)) 
        entropy = torch.zeros(len(action)) 
        value = torch.zeros(len(action)) 
        action = action.long()
        return action, log_prob, entropy, value

    def get_value(self, x):
        return torch.zeros(len(x))
    
class PPO_metaworld_Agent(nn.Module):
    def __init__(self, envs, seed=None,hidden_size=256):
        set_seed(seed)
        envs.unwrapped.seed(seed)
        envs.action_space.seed(seed)
        envs.observation_space.seed(seed)

        super().__init__()
        self.critic = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.observation_space.shape).prod(), hidden_size)),
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, hidden_size)),
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, 1), std=1.0),
        )
        self.actor_mean = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.observation_space.shape).prod(), hidden_size)),
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, hidden_size)),
            nn.Tanh(),
            layer_init(nn.Linear(hidden_size, np.prod(envs.action_space.shape)), std=0.01),
        )
        self.actor_logstd = nn.Parameter(torch.zeros(np.prod(envs.action_space.shape))) 
 
        self.init_critic_params = self.get_flat_params(self.critic).detach()
        self.init_actor_mean_params = self.get_flat_params(self.actor_mean).detach()
        self.init_actor_logstd = self.actor_logstd.detach()

    def get_value(self, x):
        return self.critic(x)
        
    def set_flat_params(self):
        self.init_critic_params = self.get_flat_params(self.critic).detach()
        self.init_actor_params = self.get_flat_params(self.actor).detach()

    def get_flat_params(self, module):
        return torch.cat([p.flatten() for p in module.parameters()])
    
    def compute_l2_loss(self, device= torch.device("cuda" if torch.cuda.is_available() else "cpu")):
        curr_critic_params = self.get_flat_params(self.critic).to(device)
        curr_actor_mean_params = self.get_flat_params(self.actor_mean).to(device)

        l2_loss_critic = 0.5 * ((curr_critic_params - self.init_critic_params.to(device)) ** 2).sum()
        l2_loss_actor = 0.5 * ((curr_actor_mean_params - self.init_actor_mean_params.to(device)) ** 2).sum()
        l2_loss_actor_logstd = 0.5 * ((self.actor_logstd.to(device) - self.init_actor_logstd.to(device)) ** 2).sum()

        l2_loss = l2_loss_critic + l2_loss_actor + l2_loss_actor_logstd
        
        return l2_loss
    
    def get_action_and_value(self, x, action=None):
        action_mean = self.actor_mean(x)
        action_logstd = self.actor_logstd
        action_std = torch.exp(action_logstd)
        probs = Normal(action_mean, action_std)
        if action is None:
            action = probs.sample()
        log_prob = probs.log_prob(action)
        entropy = probs.entropy()

        if log_prob.dim() >=1:
            log_prob_sum = log_prob.sum().unsqueeze(0)
        else:
            log_prob_sum = log_prob.sum(1)
        if entropy.dim() >=1:
            entropy_sum = entropy.sum().unsqueeze(0)
        else:
            entropy_sum = entropy.sum(1)
        return action, log_prob_sum, entropy_sum, self.critic(x).unsqueeze(0)

class PPO_Conv_Agent(nn.Module):
    def __init__(self, envs, hidden_size=64, seed=None, use_crelu=False, 
    use_DiagonalLayer=False, use_inputScaling=False, use_clip_l2=False, beta=2.0,
    use_rehearsal=False):
        super(PPO_Conv_Agent, self).__init__()
        self.use_crelu = use_crelu
        self.use_DiagonalLayer= use_DiagonalLayer
        self.use_inputScaling=use_inputScaling
        self.use_clip_l2=use_clip_l2
        self.use_rehearsal=use_rehearsal
        if self.use_rehearsal:
            self.game_buffers = {}
        if self.use_clip_l2:
            self.beta = beta
            self.init_bounds = InitBounds()
        if seed is not None:
            torch.manual_seed(seed)
        if self.use_crelu:
            self.fc_activation_fn = CReLU()
            self.conv_activation_fn = ConvCReLU() 
        input_channels = envs.single_observation_space.shape[0] 
        num_actions = envs.single_action_space.n

        self.conv = nn.Conv2d(in_channels=input_channels, out_channels=32, kernel_size=2)
        self.pool = nn.AvgPool2d(kernel_size=2, stride=2)
        self.fc1 = nn.Linear(512, hidden_size)
        if self.use_crelu:
            self.fc1 = nn.Linear(512*2, hidden_size)
            self.actor_fc1 = nn.Linear(hidden_size * 2, hidden_size)
            self.actor_fc2 = nn.Linear(hidden_size * 2, hidden_size)
            self.actor_out = nn.Linear(hidden_size * 2, num_actions)

            self.critic_fc1 = nn.Linear(hidden_size * 2, hidden_size)
            self.critic_fc2 = nn.Linear(hidden_size * 2, hidden_size)
            self.critic_out = nn.Linear(hidden_size * 2, 1)
        else:
            self.actor_fc1 = nn.Linear(hidden_size, hidden_size)
            self.actor_fc2 = nn.Linear(hidden_size, hidden_size)
            self.actor_out = nn.Linear(hidden_size, num_actions)

            self.critic_fc1 = nn.Linear(hidden_size, hidden_size)
            self.critic_fc2 = nn.Linear(hidden_size, hidden_size)
            self.critic_out = nn.Linear(hidden_size, 1)

        if use_DiagonalLayer:
            self.diagonal_fc1 = DiagonalLayer(hidden_size)
            self.diagonal_actor_fc1 = DiagonalLayer(hidden_size)
            self.diagonal_actor_fc2 = DiagonalLayer(hidden_size)
            self.diagonal_critic_fc1 = DiagonalLayer(hidden_size)
            self.diagonal_critic_fc2 = DiagonalLayer(hidden_size)
        if use_inputScaling:
            self.input_scale = nn.Parameter(torch.ones(1))

        self.init_critic_params = self.get_flat_params(self.critic_fc1, self.critic_fc2, self.critic_out).detach()
        self.init_actor_params = self.get_flat_params(self.actor_fc1, self.actor_fc2, self.actor_out).detach()
        self.current_critic_target = self.init_critic_params
        self.current_actor_target = self.init_actor_params

        self.critic_param_candidates = self.sample_initial_candidates(100, mode="critic")
        self.actor_param_candidates = self.sample_initial_candidates(100, mode="actor")
        self.distance_metric = "l2"
        # self.init_params_dict = {}
        # for name, param in self.named_parameters():
        #     self.init_params_dict[name] = param.data.clone().detach()

        self.fisher_information = {}
        self.optimal_weights = {}
        
    def reset_parameters(self):
        for layer in self.modules():
            if isinstance(layer, (nn.Linear, nn.Conv2d)):
                layer.reset_parameters()
    
    def save_obs_distribution(self, game_id):
        device= torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if game_id not in self.game_buffers:
            raise ValueError(f"No observations found for game ID {game_id}.")
        buffer = self.game_buffers[game_id]
        
        new_obs = []
        idx = []
        for i, (obs, dist) in enumerate(buffer.buffer):
            if dist is None:
                new_obs.append(obs)
                idx.append(i)
        if not new_obs:
            print(f"No observations with missing distributions for game ID {game_id}.")
            return

        sub_batch_size = 4
        self.game_buffers[game_id]=RehearsalBuffer(buffer_size=100000)
        for start_idx in range(0, len(new_obs), sub_batch_size):
            obs_batch = torch.stack(new_obs[start_idx:start_idx + sub_batch_size])
            with torch.no_grad():
                batch_size, num_envs = obs_batch.shape[:2]
                reshaped_obs = obs_batch.view(batch_size * num_envs, *obs_batch.shape[2:])
                reshaped_obs=reshaped_obs.to(device)
                logits, _ = self.forward(reshaped_obs)
                distributions = logits.detach().cpu()
            
            for obs, dist in zip(reshaped_obs.cpu(), distributions):
                self.game_buffers[game_id].add(obs, dist)

    def add_obs(self, game_id, obs, dist=None):
        if game_id not in self.game_buffers:
            self.game_buffers[game_id]=RehearsalBuffer(buffer_size=100000)
        self.game_buffers[game_id].add(obs.clone().detach().cpu(), dist.cpu() if dist is not None else dist)

    def sample_uniform_per_game(self, curr_game_id, batch_size):
        previous_game_ids = range(0, curr_game_id)
        buffers = [
                self.game_buffers[game_id]
                for game_id in previous_game_ids if game_id in self.game_buffers
            ]

        if not buffers:
            raise ValueError(f"No buffers found for previous games for curr_game_id={curr_game_id}")
        # uniform probability for sample from combined buffer
        num_games = curr_game_id
        samples_per_game = batch_size // num_games
        extra_samples = batch_size % num_games

        sampled_obs = []
        sampled_distri = []
        for i, buffer in enumerate(buffers):
            obs, distri = zip(*buffer.buffer)
            obs = torch.stack(obs)
            distri = torch.stack(distri)
            num_samples = samples_per_game + (1 if i < extra_samples else 0)

            indices = torch.randperm(obs.size(0))[:num_samples]

            sampled_obs.append(obs[indices])
            sampled_distri.append(distri[indices])

        obs_batch = torch.cat(sampled_obs, dim=0)
        distri_batch = torch.cat(sampled_distri, dim=0)

        return obs_batch, distri_batch

    def perform_rehearsal_loss(self, obs_batch, distributions):
        device= torch.device("cuda" if torch.cuda.is_available() else "cpu")
        obs_batch = obs_batch.to(device)
        distributions = distributions.to(device)
        # reshape: combine batch and environment dims
        total_loss = 0

        sub_batch_size = 4
        sub_batches_obs = obs_batch.split(sub_batch_size)
        sub_batches_dist = distributions.split(sub_batch_size)

        for sub_batch_obs, sub_batch_dist in zip(sub_batches_obs, sub_batches_dist):
            logits, _ = self.forward(sub_batch_obs)
            loss = F.cross_entropy(logits, sub_batch_dist.argmax(dim=-1))
            total_loss += loss

        return total_loss
        
    def sample_initial_candidates(self, num_samples, mode="critic"):
        candidates = []
        for _ in range(num_samples):
            model = copy.deepcopy(self)
            model.reset_parameters()
                # if param.requires_grad:
                    # if mode in name:
                    #     if param.dim() > 1:
                    #         nn.init.xavier_uniform_(param)
                    #     else:
                    #         nn.init.zeros_(param)
                    #                 if param.requires_grad and mode in name:
                    # if param.dim() > 1:
                    #     nn.init.kaiming_uniform_(param, a=math.sqrt(5))
                    # else:
                    #     fan_in, _ = nn.init._calculate_fan_in_and_fan_out(param)
                    #     bound = 1 / math.sqrt(fan_in) if fan_in > 0 else 0
                    #     nn.init.uniform_(param, -bound, bound)
            if mode == "critic":
                candidates.append(self.get_flat_params(
                    model.critic_fc1, model.critic_fc2, model.critic_out).detach())
            elif mode == "actor":
                candidates.append(self.get_flat_params(
                    model.actor_fc1, model.actor_fc2, model.actor_out).detach())
        return candidates

    def compute_distance(self, params1, params2, metric="l2"):
        device = params1.device
        params2 = params2.to(device)
        if metric == "l2":
            return torch.norm(params1 - params2).item()
        elif metric == "cosine":
            return 1 - torch.nn.functional.cosine_similarity(params1, params2, dim=0).item()

    def set_distance_metric(self, metric):
        if metric not in ["l2", "cosine"]:
            raise ValueError("Unsupported metric!")
        self.distance_metric = metric
    
    def set_l2_target(self):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        current_critic_params = self.get_flat_params(self.critic_fc1, self.critic_fc2, self.critic_out).detach().to(device)
        current_actor_params = self.get_flat_params(self.actor_fc1, self.actor_fc2, self.actor_out).detach().to(device)

        critic_distances = [
            self.compute_distance(current_critic_params, c, metric=self.distance_metric)
            for c in self.critic_param_candidates
        ]
        actor_distances = [
            self.compute_distance(current_actor_params, c, metric=self.distance_metric)
            for c in self.actor_param_candidates
        ]

        min_critic_dist=min(critic_distances)
        min_actor_dist=min(actor_distances)
        self.current_critic_target = self.critic_param_candidates[critic_distances.index(min_critic_dist)].to(device)
        self.current_actor_target = self.actor_param_candidates[actor_distances.index(min_actor_dist)].to(device)
        return min_critic_dist, min_actor_dist

    def set_flat_params(self):
        self.init_critic_params = self.get_flat_params(self.critic_fc1, self.critic_fc2, self.critic_out).detach()
        self.init_actor_params = self.get_flat_params(self.actor_fc1, self.actor_fc2, self.actor_out).detach()
        self.current_critic_target = self.init_critic_params
        self.current_actor_target = self.init_actor_params

    def forward(self, x):
        if self.use_inputScaling:
            x = x * self.input_scale

        if self.use_crelu:
            x = self.conv_activation_fn(self.conv(x))
            x = self.pool(x)
            x = x.view(x.size(0), -1)
            x = self.fc_activation_fn(self.fc1(x))

            actor_mean = self.fc_activation_fn(self.actor_fc1(x))
            actor_mean = self.fc_activation_fn(self.actor_fc2(actor_mean))
            actor_mean = self.actor_out(actor_mean)

            critic_value = self.fc_activation_fn(self.critic_fc1(x))
            critic_value = self.fc_activation_fn(self.critic_fc2(critic_value))
            critic_value = self.critic_out(critic_value)
        elif self.use_DiagonalLayer:
            x = F.relu(self.conv(x))
            x = self.pool(x)

            x = x.view(x.size(0), -1)
            x = self.diagonal_fc1(F.relu(self.fc1(x)))

            actor_mean = self.diagonal_actor_fc2(F.relu(self.actor_fc2(
                            self.diagonal_actor_fc1(F.relu(self.actor_fc1(x))))))
            actor_mean = self.actor_out(actor_mean)

            critic_value = self.diagonal_critic_fc2(F.relu(self.critic_fc2(
                            self.diagonal_critic_fc1(F.relu(self.critic_fc1(x))))))
            critic_value = self.critic_out(critic_value)
        else:
            x = F.relu(self.conv(x))
            x = self.pool(x)

            x = x.view(x.size(0), -1)
            x = F.relu(self.fc1(x))

            actor_mean = F.relu(self.actor_fc1(x))
            actor_mean = F.relu(self.actor_fc2(actor_mean))
            actor_mean = self.actor_out(actor_mean)

            critic_value = F.relu(self.critic_fc1(x))
            critic_value = F.relu(self.critic_fc2(critic_value))
            critic_value = self.critic_out(critic_value)

        return actor_mean, critic_value

    def get_value(self, x):
        _, value = self.forward(x)
        return value

    def get_action_and_value(self, x, action=None):
        logits, value = self.forward(x)
        probs = Categorical(logits=logits)
        
        if action is None:
            action = probs.sample()
            
        return action, probs.log_prob(action), probs.entropy(), value

    def get_flat_params(self, *modules):
        params = []
        for module in modules:
            params.append(torch.cat([p.flatten().clone() for p in module.parameters()]))
        return torch.cat(params)
    # def get_flat_params(self):
        
    #     self.init_params_dict = {}
    #     for name, param in self.named_parameters():
    #         self.init_params_dict[name] = param.data.clone().detach()
    def compute_l2_0_loss(self):
        l2_0_loss = 0.0
        for name, param in self.named_parameters():
            if not param.requires_grad or "layer_norm" in name or \
                "init_params" in name or \
                    "original_last_layer_params" in name:
                continue
            l2_0_loss += torch.sum(param ** 2)
        return 0.5 * l2_0_loss
    
    def compute_clipping_l2_loss(self):
        l2_0_loss = 0.0
        for name, param in self.named_parameters():
            if not param.requires_grad or "layer_norm" in name or \
               "init_params" in name or "original_last_layer_params" in name:
                continue

            bound = self.init_bounds.get(param)
            mask = (param.abs() > self.beta * bound).float()
            l2_0_loss += torch.sum(mask * (param ** 2))
        return 0.5 * l2_0_loss

    def compute_l2_loss(self, device=torch.device("cuda" if torch.cuda.is_available() else "cpu")):
        curr_critic_params = self.get_flat_params(self.critic_fc1, self.critic_fc2, self.critic_out)
        curr_actor_params = self.get_flat_params(self.actor_fc1, self.actor_fc2, self.actor_out).detach()

        l2_loss_critic = 0.5 * ((curr_critic_params - self.current_critic_target.to(device)) ** 2).sum()
        l2_loss_actor = 0.5 * ((curr_actor_params - self.current_actor_target.to(device)) ** 2).sum()

        l2_loss = l2_loss_critic + l2_loss_actor
        
        return l2_loss_critic, l2_loss_actor

        # l2_loss = 0.0
        # for name, param in self.named_parameters():
        #     if param.requires_grad:
        #         init_param = self.init_params_dict[name].to(device)
        #         #L2 distance between current and initial parameters
        #         diff = param.to(device) - init_param
        #         l2_loss += torch.sum(diff ** 2)
        # return 0.5 * l2_loss

    def parseval_regularization(self,s=2):
        parseval_loss = 0.0

        for layer in [self.fc1,self.actor_fc1, self.actor_fc2,self.critic_fc1, self.critic_fc2]:
            W = layer.weight
            identity = torch.eye(W.size(0), device=W.device)
            parseval_loss += torch.norm(W @ W.T - s * identity, p="fro") ** 2

        return parseval_loss

    def compute_fisher_information(self, sampled_obs, sampled_actions):
        fisher_accumulated = {name: torch.zeros_like(param) for name, param in self.named_parameters() if param.requires_grad}
        for obs, action in zip(sampled_obs, sampled_actions):
            self.zero_grad()
            _, log_prob, _, _ = self.get_action_and_value(obs.unsqueeze(0), action.unsqueeze(0))
            log_prob.backward()

            for name, param in self.named_parameters():
                if param.grad is not None:
                    fisher_accumulated[name] += (param.grad.data ** 2).clone()
        # Normalize by the number of samples
        for name in fisher_accumulated:
            fisher_accumulated[name] /= len(sampled_obs)
        for name, fisher_value in fisher_accumulated.items():
            if name not in self.fisher_information:
                self.fisher_information[name] = fisher_value
            else:
                self.fisher_information[name] += fisher_value

    def reset_fisher_information(self):
        """Resets fisher info to start fresh for a new task."""
        self.fisher_information = {name: torch.zeros_like(param) 
                                   for name, param in self.named_parameters()}

    def store_optimal_weights(self):
        self.optimal_weights = {
            name: param.data.clone() for name, param in self.named_parameters()
        }

    def ewc_loss(self):
        ewc_loss = 0
        for name, param in self.named_parameters():
            if name in self.fisher_information:
                ewc_loss += (self.fisher_information[name] *
                             (param - self.optimal_weights[name]) ** 2).sum()
        return ewc_loss

class PPO_Conv_Agent_VCL(nn.Module):
    def __init__(self, envs, hidden_size=64, cl_reg_coef=1, seed=None):
        if seed is not None:
            torch.manual_seed(seed)
        super().__init__()
        input_channels = envs.single_observation_space.shape[0]
        num_actions = envs.single_action_space.n

        self.conv = nn.Conv2d(input_channels, 32, kernel_size=2)
        self.pool = nn.AvgPool2d(kernel_size=2, stride=2)

        self.fc1 = BayesianLayer(512, hidden_size)
        self.actor_fc1 = BayesianLayer(hidden_size, hidden_size)
        self.actor_out = BayesianLayer(hidden_size, num_actions)
        self.critic_fc1 = BayesianLayer(hidden_size, hidden_size)
        self.critic_out = BayesianLayer(hidden_size, 1)

        self.cl_reg_coef = cl_reg_coef

    def forward(self, x):
        x = F.relu(self.conv(x))
        x = self.pool(x)
        x = x.view(x.size(0), -1)
        x = F.relu(self.fc1(x))

        actor_mean = F.relu(self.actor_fc1(x))
        actor_mean = self.actor_out(actor_mean)

        critic_value = F.relu(self.critic_fc1(x))
        critic_value = self.critic_out(critic_value)

        return actor_mean, critic_value
        
    def get_value(self, x):
        _, value = self.forward(x)
        return value
        
    def get_action_and_value(self, x, action=None):
        logits, value = self.forward(x)
        probs = Categorical(logits=logits)

        if action is None:
            action = probs.sample()

        return action, probs.log_prob(action), probs.entropy(), value

    def compute_kl_loss(self):
        kl_loss = (
            self.fc1.kl_divergence() +
            self.actor_fc1.kl_divergence() +
            self.actor_out.kl_divergence() +
            self.critic_fc1.kl_divergence() +
            self.critic_out.kl_divergence()
        )
        return self.cl_reg_coef * kl_loss

    def update_priors(self):
        self.fc1.update_prior()
        self.actor_fc1.update_prior()
        self.actor_out.update_prior()
        self.critic_fc1.update_prior()
        self.critic_out.update_prior()

class BayesianLayer(nn.Module):
    def __init__(self, input_dim, output_dim, init_logvar=-6.0):
        super().__init__()
        self.input_dim = input_dim
        self.output_dim = output_dim

        self.posterior_mean = nn.Parameter(torch.zeros(input_dim, output_dim))
        self.posterior_logvar = nn.Parameter(torch.full((input_dim, output_dim), init_logvar))

        self.prior_mean = torch.zeros(input_dim, output_dim, requires_grad=False)
        self.prior_logvar = torch.full((input_dim, output_dim), init_logvar, requires_grad=False)

    def forward(self, x):
        eps = torch.randn_like(self.posterior_mean)
        weights = self.posterior_mean + eps * torch.exp(0.5 * self.posterior_logvar)
        return x @ weights

    def update_prior(self):
        with torch.no_grad():
            self.prior_mean.copy_(self.posterior_mean)
            self.prior_logvar.copy_(self.posterior_logvar)

    def kl_divergence(self, device= torch.device("cuda" if torch.cuda.is_available() else "cpu")):
        posterior_var = torch.exp(self.posterior_logvar)
        prior_var = torch.exp(self.prior_logvar)

        prior_var = prior_var.to(device)
        posterior_var = posterior_var.to(device)
        self.posterior_mean = self.posterior_mean.to(device)
        self.prior_mean = self.prior_mean.to(device)
        
        kl = 0.5 * (torch.log(prior_var) - torch.log(posterior_var) +
                    (posterior_var + (self.posterior_mean - self.prior_mean)**2) / prior_var - 1)
        return kl.sum()


class PackNet(nn.Module):
    def __init__(self, model, task_id, total_task_num):
        super().__init__()
        device="cuda" if torch.cuda.is_available() else "cpu"
        self.model = model
        self.task_id = task_id
        self.total_task_num = total_task_num
        self.prune_percentage = 1 / total_task_num

        self.masks = []
        for name, param in self.model.named_parameters():
            if name.endswith(".weight"):
                self.masks.append(
                    torch.zeros(param.size(), dtype=torch.long, device=device)
                )
            else:
                self.masks.append(None)
        self.view = None
        self.handled_layers = (
            []
        )

    @torch.no_grad()
    def adjust_gradients(self, retrain_mode=False):
        mask_id = self.task_id if retrain_mode else 0
        for p, mask in zip(self.model.parameters(), self.masks):
            if mask is None:
                continue
            p.grad = p.grad * (mask == mask_id)

    @torch.no_grad()
    def prune(self):
        for p, mask in zip(self.model.parameters(), self.masks):
            if mask is not None:
                # sort the unassigned weights from lower to higher magnitudes
                masked = p * (mask == 0)  # only select "free" weights
                flat = masked.flatten()
                _sorted, idx = torch.sort(
                    flat.abs(), descending=True
                )  # sort from max to min magnitude
                n_prune = int(
                    self.prune_percentage * flat.size(0)
                )  # number of weights to keep in pruning

                # create the mask
                mask.flatten()[idx[:n_prune]] = self.task_id

    @torch.no_grad()
    def apply_mask(self):
        for param, mask in zip(self.model.parameters(), self.masks):
            if mask is not None:
                param.data *= mask.float()

    @torch.no_grad()
    def set_view(self, task_id):
        if task_id is None and self.view is not None:
            # restore the original state of the model in the free parameters (not masked)
            for param_copy, param, mask in zip(
                self.handled_layers, self.model.parameters(), self.masks
            ):
                if param_copy is None:
                    continue
                m = torch.logical_and(
                    mask <= self.view, mask > 0
                )  # pruned=0, not-pruned=1
                param.data += param_copy.data * torch.logical_not(m)

            self.handled_layers = []
            self.view = task_id
            return

        if len(self.handled_layers) == 0:
            # save a copy of each (parametrized) layer of the model
            for param, mask in zip(self.model.parameters(), self.masks):
                if mask is not None:
                    self.handled_layers.append(copy.deepcopy(param))
                else:
                    self.handled_layers.append(None)

        # apply the masks
        for p, mask in zip(self.model.parameters(), self.masks):
            if mask is None:
                continue
            # set to zero the parameters that are free (have no mask) or whose mask ID is greater than task_id
            p.data *= torch.logical_and(mask <= task_id, mask > 0)

        self.view = task_id

    def forward(self, x):
        return self.model(x)

class PPO_PackNet_Agent(nn.Module):
    def __init__(self, envs, task_id=1, total_task_num=5,seed=None):
        super().__init__()
        self.model=PPO_Conv_Agent(envs=envs,seed=seed)
        self.packnet = PackNet(model=self.model, task_id=task_id, total_task_num=total_task_num)
        self.retrain_mode = False

    def get_value(self, x):
        return self.model.get_value(x)

    def get_action_and_value(self, x, action=None):
        return self.model.get_action_and_value(x, action)

    def prune(self):
        self.packnet.prune()

    def apply_mask(self):
        self.packnet.apply_mask()

    def set_view(self, task_id):
        self.packnet.set_view(task_id)

    def forward(self, x):
        return self.packnet(x)
    
    def start_retraining(self):
        if self.retrain_mode:
            return  # nothing to do

        print("==> PackNet re-training starts!")

        self.retrain_mode = True
        self.packnet.prune()  # generate the masks for the current task
        self.packnet.set_view(self.packnet.task_id)

    def before_update(self):
        self.packnet.adjust_gradients(retrain_mode=self.retrain_mode)


class RehearsalBuffer:
    def __init__(self, buffer_size):
        self.buffer = []
        self.buffer_size = buffer_size

    def add(self, obs, distribution):
        if len(self.buffer) >= self.buffer_size:
            self.buffer.pop(0)
        self.buffer.append((obs, distribution))

    def sample(self, batch_size):
        samples = random.sample(self.buffer, min(batch_size, len(self.buffer)))
        obs_batch, logits_batch = zip(*samples)
        return torch.stack(obs_batch), torch.stack(logits_batch)

class InitBounds:
    '''
    A class to calculate the initial bounds for weight clipping.
    Uniform Kaiming initialization bounds are used.
    Since bias requires knowledge of the previous layer's weights, we keep track of the previous weight tensor in this class.
    Linear: https://github.com/pytorch/pytorch/blob/main/torch/nn/modules/linear.py#L106
    Conv2d: https://github.com/pytorch/pytorch/blob/main/torch/nn/modules/conv.py#L144
    '''
    def __init__(self):
        self.previous_weight = None

    def get(self, p):
        if p.dim() == 1:
            fan_in, _ = torch.nn.init._calculate_fan_in_and_fan_out(self.previous_weight)
            return 1.0 / math.sqrt(fan_in)
        elif p.dim() == 2 or p.dim() == 4:
            self.previous_weight = p
            fan_in, _ = torch.nn.init._calculate_fan_in_and_fan_out(p)
            return  1.0 / math.sqrt(fan_in)
        else:
            raise ValueError("Unsupported tensor dimension: {}".format(p.dim()))

class WeightClipping(torch.optim.Optimizer):
    def __init__(self, params, beta=1.0, optimizer=torch.optim.Adam, clip_last_layer=True, **kwargs):
        defaults = dict(beta=beta, clip_last_layer=clip_last_layer)
        super(WeightClipping, self).__init__(params, defaults)
        self.optimizer = optimizer(self.param_groups, **kwargs)
        self.param_groups = self.optimizer.param_groups
        self.defaults.update(self.optimizer.defaults)
        self.init_bounds = InitBounds()

    def step(self):
        self.optimizer.step()
        self.weight_clipping()

    def weight_clipping(self):
        for group in self.param_groups:
            for i, p in enumerate(group["params"]):
                if i >= len(group["params"])-2 and not group["clip_last_layer"]:
                    # do not clip last layer of weights/biases
                    continue
                bound = self.init_bounds.get(p)
                p.data.clamp_(-group["beta"] * bound, group["beta"] * bound)