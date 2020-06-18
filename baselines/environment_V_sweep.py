# -*- coding: utf-8 -*-
import csv
import logging
import pathlib
import random

import numpy as np
import collections

from baselines.servernode_w_appqueue_w_appinfo_cores import ServerNode as Edge
from baselines.servernode_w_totalqueue_cores import ServerNode as Cloud
from baselines.environment import Environment
from baselines.applications import *
from baselines.channels import *
from baselines.constants import *
from baselines.rl_networks.utils import *
import gym
from gym import error, spaces
from gym.utils import seeding

import time

class MEC_v1(gym.Env):
    def __init__(self, task_rate=10, applications=(SPEECH_RECOGNITION, NLP, FACE_RECOGNITION), time_delta=1, use_beta=True, empty_reward=True, cost_type=COST_TYPE, max_episode_steps=5000):
        super().__init__()

        self.state_dim= 0
        self.action_dim= 0
        self.clients = dict()
        self.servers = dict()
        self.links = list()
        self.timestamp = 0
        self.silence = True

        self.applications = applications
        self.task_rate = task_rate#/time_delta
        self.reset_info = list()
        self.use_beta = use_beta
        self.empty_reward = empty_reward
        self.cost_type = cost_type
        self.max_episode_steps = max_episode_steps


        channel = WIRED

        edge_capability = NUM_EDGE_CORES * NUM_EDGE_SINGLE * GHZ
        cloud_capability = NUM_CLOUD_CORES * NUM_CLOUD_SINGLE * GHZ
        self.reset_info.append((edge_capability, cloud_capability, channel))
        state = self.init_linked_pair(edge_capability, cloud_capability, channel)
        self.obs_dim = state.size

        # import pdb;pdb.set_trace()
        high = np.inf*np.ones(self.action_dim)
        low = -high
        self.action_space = spaces.Box(low, high)
        self.action_dim = 0

        high = np.inf*np.ones(self.obs_dim)
        low = -high
        self.observation_space = spaces.Box(low, high)

        self.clients = dict()
        self.servers = dict()
        self.links = list()
        self._seed()

    def _seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    # methods to override:
    # ----------------------------
    def init_linked_pair(self, edge_capability, cloud_capability, channel):
        client = self.add_client(edge_capability)
        client.make_application_queues(*self.applications)

        server = self.add_server(cloud_capability)

        self.add_link(client, server, channel)

        # self.reset_info.append((edge_capability, cloud_capability, channel))
        state = self.get_status()

        self.state_dim = len(state)
        self.action_dim += len(client.get_applications())+1
        if self.use_beta:
            # if the client use several servers later, action_dim should be increased.
            self.action_dim *=2
        return state

    def add_client(self, cap):
        client = Edge(cap, True)
        self.clients[client.get_uuid()] = client
        return client

    def add_server(self, cap):
        server = Cloud(cap)
        self.servers[server.get_uuid()] = server
        return server

    def add_link(self, client, server, up_channel, down_channel=None):
        up_channel = Channel(up_channel)
        if not down_channel:
            down_channel = Channel(up_channel)
        else:
            down_channel = Channel(down_channel)

        client.links_to_higher[server.get_uuid()]= {
            'node' : server,
            'channel' : up_channel
        }
        server.links_to_lower[client.get_uuid()] = {
            'node' : client,
            'channel' : down_channel
        }
        self.links.append((client.get_uuid(), server.get_uuid()))
        return

    def get_number_of_apps(self):
        return len(self.applications)

    def __del__(self):
        for k in list(self.clients.keys()):
            del self.clients[k]
        for k in list(self.servers.keys()):
            del self.servers[k]
        del self.links
        del self.applications
        # del self.reset_info

    def reset(self, empty_reward=True, rand_start = 0):
        t1 = time.time()
        task_rate = self.task_rate
        applications = self.applications
        # reset_info = self.reset_info
        use_beta = self.use_beta
        cost_type = self.cost_type
        self.__del__()
        self.__init__(task_rate, applications, use_beta = use_beta, empty_reward=empty_reward, cost_type=cost_type)
        for reset_info in self.reset_info:
            self.init_linked_pair(*reset_info)
        reset_state = self.get_status(scale=GHZ)
        t2 = time.time()
        # print("reset time : ",t2-t1)
        return reset_state

    def get_status(self, scale=GHZ):
        edge_state, cloud_state, link_state = list(), list(), list()
        for client in self.clients.values():
            temp_state = client.get_status(self.timestamp, scale=scale)
            edge_state += temp_state

        state = edge_state
        if self.use_beta:
            for server in self.servers.values():
                temp_state = server.get_status(self.timestamp, scale=scale)
                cloud_state += temp_state
            for link in self.links:
                link_state.extend([self.clients[link[0]].sample_channel_rate(link[1]),self.servers[link[1]].sample_channel_rate(link[0])])

            state = edge_state + cloud_state

        return np.array(state)

    # several clients, several servers not considered. (step, _step_alpha, _step_beta)
    def step(self, action, use_beta=True, generate=True):
        t1 = time.time()
        q0, failed_to_generate, q1 = self._step_generation()
        t2 = time.time()
        # print("gen time : ",t2-t1)
        action_alpha, action_beta, usage_ratio = list(), list(), list()
        if self.use_beta:
            action_alpha = action.flatten()[:int(self.action_dim/2)].reshape(1,-1)
            action_beta = action.flatten()[int(self.action_dim/2):].reshape(1,-1)
            ### softmax here
            action_beta = softmax_1d(action_beta)
        else:
            action_alpha = action
        ### softmax here
        action_alpha = softmax_1d(action_alpha)

        # action_alpha= np.array([[0,0,0,1]])
        # action_beta = np.array([[0, 0, 0, 1]])
        used_edge_cpus, inter_state, q2 = self._step_alpha(action_alpha)
        t3 = time.time()
        # print("alp time : ",t3-t2)
        used_cloud_cpus, new_state = self._step_beta(action_beta)
        t4 = time.time()
        # print("bet time : ",t4-t3)
        cost = self.get_cost(used_edge_cpus, used_cloud_cpus, q0, q2)
        t5 = time.time()
        # print("cost time : ",t5-t4)
        # print(action_alpha)
        # print(action_beta)
        # print("asdasdasd")
        # print(used_edge_cpus)
        # print(used_cloud_cpus)
        # print(new_state)

        self.timestamp += 1
        if self.timestamp == self.max_episode_steps:
            return new_state, -cost, 1, {}
        return new_state, -cost, 0, {}

    def _step_alpha(self, action):
        # initial_qlength= self.get_total_qlength()
        used_edge_cpus = collections.defaultdict(float)
        action = action.flatten()[:-1].reshape(1,-1)
        if self.timestamp%1000==0:
            print("alpha", 1-sum(sum(action)))
        for client_id, alpha in list(zip(self.clients.keys(), action)):
            used_edge_cpus[client_id] = self.clients[client_id].do_tasks(alpha)

        state = self.get_status(scale=GHZ)
        after_qlength = self.get_edge_qlength(scale=GHZ)

        return used_edge_cpus, state, after_qlength


    def _step_beta(self, action):
        used_txs = collections.defaultdict(list)
        tasks_to_be_offloaded = collections.defaultdict(dict)
        used_cloud_cpus = collections.defaultdict(float)
        action = action.flatten()[:-1].reshape(1,-1)
        if self.timestamp%1000==0:
            print("beta", 1-sum(sum(action)))
        # 모든 client 객체에 대해 각 client의 상위 node로 offload하기
        # 각 client는 하나의 상위 노드를 가지고 있다고 가정함......?
        for client, beta in list(zip(self.clients.values(), action)):
            higher_nodes = client.get_higher_node_ids()
            for higher_node in higher_nodes:
                used_tx, task_to_be_offloaded, failed = client.offload_tasks(beta, higher_node)
                used_txs[higher_node].append(used_tx)
                tasks_to_be_offloaded[higher_node].update(task_to_be_offloaded)

        for server_id, server in self.servers.items():
            server.offloaded_tasks(tasks_to_be_offloaded[server_id], self.timestamp)
            used_cloud_cpus[server_id] = server.do_tasks()

        state = self.get_status(scale=GHZ)

        return used_cloud_cpus, state

    def _step_generation(self):
        initial_qlength= self.get_edge_qlength()
        if not self.silence: print("###### random task generation start! ######")
        for client in self.clients.values():
            arrival_size, failed_to_generate = client.random_task_generation(self.task_rate, self.timestamp, *self.applications)
        if not self.silence: print("###### random task generation ends! ######")

        after_qlength = self.get_edge_qlength(scale=GHZ)

        return initial_qlength, failed_to_generate, after_qlength

    def get_edge_qlength(self, scale=1):
        qlengths = list()
        for node in self.clients.values():
            for _, queue in node.get_queue_list():
                qlengths.append( queue.get_length(scale) )
        return qlengths

    def get_cloud_qlength(self, scale=1):
        qlengths = list()
        for node in self.servers.values():
            qlengths.append(node.get_task_queue_length(scale))
        return np.array(qlengths)


    def get_cost(self, used_edge_cpus, used_cloud_cpus, before, after, failed_to_offload=0, failed_to_generate=0):
        def compute_cost_fct(cores, cpu_usage):
            return cores*(cpu_usage/400/GHZ/cores)**3

        # power_scale = np.log10(compute_cost_fct(54,216*GHZ))
        after = np.array(after)
        thd = 0.002

        after = sum( (after>thd)*(after-thd) )

        if after>0:
            delay_scale = np.log10(after)
            # every queue lengths are in unit of  [10e9 bits]
            edge_drift_cost = (after**2)**10*(-delay_scale)
        else:
            edge_drift_cost = (after**2)

        edge_drift_cost += after

        # edge_drift_cost = edge_drift_cost*10**(power_scale-delay_scale)
        # power_scale = np.log10(compute_cost_fct(54, 214*GHZ))+1

        edge_computation_cost = 0
        for used_edge_cpu in used_edge_cpus.values():
            edge_computation_cost += compute_cost_fct(10,used_edge_cpu)


        cloud_payment_cost = 0
        for used_cloud_cpu in used_cloud_cpus.values():
            cloud_payment_cost += compute_cost_fct(54,used_cloud_cpu)

        # power_cost = (edge_computation_cost + cloud_payment_cost)**10*(-power_scale)
        # # print("asdasdasd")
        # print(used_edge_cpus)
        # print(used_cloud_cpus)
        #
        # print(edge_drift_cost)
        # print("dddd")
        # print(edge_computation_cost)
        # print(cloud_payment_cost)

        return edge_drift_cost+self.cost_type*(edge_computation_cost+cloud_payment_cost)
        # return edge_drift_cost+self.cost_type*power_cost
