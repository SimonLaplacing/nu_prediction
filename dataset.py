import os
import pickle
import sys
sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

import utils
import torch
# import intersection_dataload
import numpy as np

from utils import Json_Parser
from matplotlib import pyplot as plt
from torch.utils.data.dataset import Dataset
from nuscenes.nuscenes import NuScenes
from nuscenes.prediction.helper import PredictHelper
from nuscenes.eval.prediction.splits import get_prediction_challenge_split
from nuscenes.prediction.input_representation.agents import AgentBoxesWithFadedHistory
from nuscenes.prediction.input_representation.static_layers import StaticLayerRasterizer
from nuscenes.prediction.input_representation.interface import InputRepresentation
from nuscenes.prediction.input_representation.combinators import Rasterizer


class NuSceneDataset(Dataset):
    def __init__(self,train_mode, config_file_name, layers_list=None, color_list=None, verbose=True):
        super().__init__()
        parser = Json_Parser(config_file_name)
        config = parser.load_parser()
        
        self.verbose = verbose
        self.device = torch.device(config['LEARNING']['device'] if torch.cuda.is_available() else 'cpu')
        self.dataroot = config['DATASET']['dataset_path']
        # self.intersection_use= config['DATASET']['intersection_use']        # only available for mini_dataset
        self.nuscenes = NuScenes(config['DATASET']['dataset_str'], dataroot=self.dataroot, verbose=self.verbose)
        self.helper = PredictHelper(self.nuscenes)
        self.num_classes = config['LEARNING']['num_classes']
        self.set = config['DATASET']['set']
        self.train_mode = train_mode

        if self.set == 'train':
            # self.mode = 'train'
            self.train_set = get_prediction_challenge_split("train", dataroot=self.dataroot)
            self.val_set = get_prediction_challenge_split("val", dataroot=self.dataroot)
        else:            
            # self.mode = 'mini'
            self.train_set = get_prediction_challenge_split("mini_train", dataroot=self.dataroot)
            self.val_set = get_prediction_challenge_split("mini_val", dataroot=self.dataroot)

            # if self.intersection_use:
            #     self.train_set = intersection_dataload.token_save(self.train_set)
            #     self.val_set = intersection_dataload.token_save(self.val_set)
                
        if layers_list is None:
            self.layers_list = config['PREPROCESS']['img_layers_list']
        if color_list is None:
            self.color_list = []
            for i in range(len(self.layers_list)):
                self.color_list.append((255,255,255))

        self.resolution = config['PREPROCESS']['resolution']         
        self.meters_ahead = config['PREPROCESS']['meters_ahead']
        self.meters_behind = config['PREPROCESS']['meters_behind']
        self.meters_left = config['PREPROCESS']['meters_left']
        self.meters_right = config['PREPROCESS']['meters_right'] 

        self.num_past_hist = config['HISTORY']['num_past_hist']
        self.num_future_hist = config['HISTORY']['num_future_hist']

        self.static_layer = StaticLayerRasterizer(helper=self.helper, 
                                            layer_names=self.layers_list, 
                                            colors=self.color_list,
                                            resolution=self.resolution, 
                                            meters_ahead=self.meters_ahead, 
                                            meters_behind=self.meters_behind,
                                            meters_left=self.meters_left, 
                                            meters_right=self.meters_right)
        self.agent_layer = AgentBoxesWithFadedHistory(helper=self.helper, 
                                                seconds_of_history=self.num_past_hist, resolution=self.resolution)
        self.input_repr = InputRepresentation(static_layer=self.static_layer, 
                                        agent=self.agent_layer, 
                                        combinator=Rasterizer())     

        # self.show_imgs = config['PREPROCESS']['show_imgs']
        # self.save_imgs = config['PREPROCESS']['save_imgs']

        # self.num_max_agent = config['PREPROCESS']['num_max_agent']
        
        # self.traj_set_path = config['LEARNING']['trajectory_set_path']
        # self.trajectories_set =torch.Tensor(pickle.load(open(self.traj_set_path, 'rb')))

        # if self.save_imgs:
        #     if self.train_mode:
        #         utils.save_imgs(self, self.train_set, self.set + 'train', self.input_repr)
        #     else:
        #         utils.save_imgs(self, self.val_set, self.set + 'val', self.input_repr)
        
  
    def __len__(self):
        if self.train_mode:
            return len(self.train_set)
        else:
            return len(self.val_set)


    def get_label(self, cur_yaw, future_yaw):
        phi = np.pi / self.num_classes
        label = np.zeros(self.num_classes)
        diff = future_yaw - cur_yaw
        for k in range(self.num_classes):
            if np.pi/2-(k+1)*phi<diff<np.pi/2-k*phi:
                label[k]=1
        del phi, diff
        return label



    def __getitem__(self, idx):
        if self.train_mode:
            self.dataset = self.train_set
        else:
            self.dataset = self.val_set

        #################################### State processing ####################################
        ego_instance_token, ego_sample_token = self.dataset[idx].split('_')
        ego_annotation = self.helper.get_sample_annotation(ego_instance_token, ego_sample_token)
        ego_pose = np.array(utils.get_pose_from_annot(ego_annotation))
        # ego_vel = self.helper.get_velocity_for_agent(ego_instance_token, ego_sample_token)
        # ego_accel = self.helper.get_acceleration_for_agent(ego_instance_token, ego_sample_token)
        # ego_yawrate = self.helper.get_heading_change_rate_for_agent(ego_instance_token, ego_sample_token)
        # [ego_vel, ego_accel, ego_yawrate] = utils.data_filter([ego_vel, ego_accel, ego_yawrate])                # Filter unresonable data (make nan to zero)
        # ego_states = np.array([[ego_vel, ego_accel, ego_yawrate]])
        history = self.helper.get_past_for_agent(instance_token=ego_instance_token, sample_token=ego_sample_token, 
                                            seconds=int(self.num_past_hist/2), in_agent_frame=True, just_xy=True)
        extra = self.num_past_hist - len(history)
        for _ in range(extra):
            history = np.row_stack((history[0],history))
            
        future_position = self.helper.get_future_for_agent(instance_token=ego_instance_token, sample_token=ego_sample_token, 
                                            seconds=int(self.num_future_hist/2), in_agent_frame=True, just_xy=True)
        extra1 = self.num_future_hist - len(future_position)
        for _ in range(extra):
            future_position = np.row_stack((future_position,future_position[-1]))
        # num_future_mask = len(future_position)

        future = self.helper.get_future_for_agent(instance_token=ego_instance_token, sample_token=ego_sample_token, 
                                            seconds=int(self.num_future_hist/2), in_agent_frame=False,just_xy=False)
        final_instance_token, final_sample_token = future[-1]['instance_token'],  future[-1]['sample_token']                                   
        final_annotation = self.helper.get_sample_annotation(final_instance_token, final_sample_token)                                    
        future_pose = np.array(utils.get_pose_from_annot(final_annotation))

        ## Get label
        label = self.get_label(ego_pose[-1], future_pose[-1])
        # print(self.trajectories_set.size())
        # print(label)
        #################################### Image processing ####################################
        img = self.input_repr.make_input_representation(instance_token=ego_instance_token, sample_token=ego_sample_token)
        # if self.show_imgs:
        #     plt.figure('input_representation')
        #     plt.imshow(img)
        #     plt.show()

        # img = torch.Tensor(img).permute(2,0,1).to(device=self.device)
        del ego_annotation, ego_pose, extra, future, final_instance_token, final_sample_token, final_annotation, extra1, 


        return {'image'                : img,                          # Type : torch.Tensor
                # 'ego_cur_pos'          : ego_pose,                     # Type : np.array([global_x,globa_y,global_yaw])                        | Shape : (3, )
                # 'ego_state'            : ego_states,                   # Type : np.array([[vel,accel,yaw_rate]]) --> local(ego's coord)   |   Unit : [m/s, m/s^2, rad/sec]
                'history_positions'    : history,    
                'target_positions'     : future_position,                       # Type : np.array([local_x, local_y, local_yaw]) .. ground truth data
                # 'num_future_mask'      : num_future_mask,              # a number for masking future history
                'label'                : label,                        # calculated label data from preprocessed_trajectory_set using ground truth data
                'instance'             : ego_instance_token,
                'sample'               : ego_sample_token
                }

    
# if __name__ == '__main__':
#     ## train dataset
#     # train_dataset = NuSceneDataset_CoverNet(train_mode=True, config_file_name='./covernet_config.json', verbose=True)
#     # print(len(train_dataset))
#     # print(train_dataset.__len__())

#     # val dataset
#     val_dataset = NuSceneDataset(train_mode=True, config_file_name='./config.json', verbose=True)
#     # trajectories_set =torch.Tensor(pickle.load(open("./trajectory-sets/epsilon_8.pkl", 'rb')))

#     print(len(val_dataset))
#     for i in range(val_dataset.__len__()):
#         d= val_dataset.__getitem__(i)

#     # val_dataset.__getitem__(0)

#         # for plotting
#         xs = []
#         ys = []
#         for j in range(len(d['future_local_ego_pos'])):
#             xs.append(d['future_local_ego_pos'][j][0])
#             ys.append(d['future_local_ego_pos'][j][1])
#         xs = np.array(xs)
#         ys = np.array(ys)

#         xss = []
#         yss = []
#         # label = trajectories_set[d['label'],:,:]
#         # for j in range(len(label)):
#         #     xss.append(label[j][0])
#         #     yss.append(label[j][1])
#         xss = np.array(xss)
#         yss = np.array(yss)


#         fig, ax = plt.subplots(1,3, figsize = (10,10))
#         # Rasterized Image
#         ax[0].imshow(d['img'])
#         ax[0].set_title("Rasterized Image")
#         # Real ego future history
#         ax[1].set_title("Real ego future history")
#         ax[1].plot(xs, ys, 'bo')
#         ax[1].set_aspect('equal')
#         ax[1].set_xlim(-30, 30)
#         ax[1].set_ylim(-10, 50)
#         # Label of traj_set
#         ax[2].plot(xss,yss,'yo')
#         ax[2].set_aspect('equal')
#         ax[2].set_xlim(-30,30)
#         ax[2].set_ylim(-10,50)
        # ax[2].set_title("{}th anchor".format(d['label']))
