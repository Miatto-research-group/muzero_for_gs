import datetime
import os
import gym
import numpy as np
import torch
from .abstract_game import AbstractGame
from collections import deque
import itertools
from games.gs_utils.operators import *
from games.gs_utils.utils import make_random_unitary, make_full_actions_list

SIZE = len(make_full_actions_list(QB1GATES, [], 1))

###############################################################################
##################################  MUZERO  ###################################
###############################################################################
class MuZeroConfig:
    def __init__(self, nb_actions=SIZE, observation_shape=(2, 2, 2), seed:int=42, nb_players:int=1, max_num_gpus=None):
        """
        More information is available here: https://github.com/werner-duvaud/muzero-general/wiki/Hyperparameter-Optimization
        Parameters
        ----------
        observation_shape : (2^nb_qbs, 2^nb_qbs, 2)
            dimensions of the game observation, must be 3D (channel, height, width). For a 1D array, please reshape it to (1, 1, length of array)
            (2, 2, 2) for 1qb system
            (4, 4, 2)  for 2qb system
        seed :
        """

        self.seed = seed  # Seed for numpy, torch and the game
        self.max_num_gpus = max_num_gpus  # Fix the maximum number of GPUs to use. It's usually faster to use a single GPU (set it to 1) if it has enough memory. None will use every GPUs available
        self.observation_shape = observation_shape
        self.action_space = list(range(nb_actions))
        self.players = list(range(nb_players))  # List of players. You should only edit the length
        self.stacked_observations = 0  # Number of previous observations and previous actions to add to the current observation

        # Evaluate
        self.muzero_player = 0  # Turn Muzero begins to play (0: MuZero plays first, 1: MuZero plays second)
        self.opponent = None  # Hard coded agent that MuZero faces to assess his progress in multiplayer games. It doesn't influence training. None, "random" or "expert" if implemented in the Game class

        ### Self-Play
        self.num_workers = 1  # Number of simultaneous threads/workers self-playing to feed the replay buffer
        self.selfplay_on_gpu = False

        #!!!3 heuristic decisions
        self.max_moves = 1000  # Maximum number of moves if game is not finished before
        self.num_simulations = 50  # Number of future moves self-simulated
        self.discount = 0.999  # Chronological discount of the reward

        self.temperature_threshold = None  # Number of moves before dropping the temperature given by visit_softmax_temperature_fn to 0 (ie selecting the best action). If None, visit_softmax_temperature_fn is used every time

        # Root prior exploration noise
        self.root_dirichlet_alpha = 0.25
        self.root_exploration_fraction = 0.25

        # UCB formula
        self.pb_c_base = 19652
        self.pb_c_init = 1.25

        ### Network
        #Heuristic choice !!!
        self.network = "fullyconnected"  # "resnet" / "fullyconnected"
        self.support_size = 10  # Value and reward are scaled (with almost sqrt) and encoded on a vector with a range of -support_size to support_size. Choose it so that support_size <= sqrt(max(abs(discounted reward)))
        
        # Residual Network
        self.downsample = False  # Downsample observations before representation network, False / "CNN" (lighter) / "resnet" (See paper appendix Network Architecture)
        self.blocks = 1  # Number of blocks in the ResNet

        #Heuristics choices!!! should be the same number for all 4
        self.channels = 2  # Number of channels in the ResNet
        self.reduced_channels_reward = 2  # Number of channels in reward head
        self.reduced_channels_value = 2  # Number of channels in value head
        self.reduced_channels_policy = 2  # Number of channels in policy head

        #Heuristic choices!!! should be same for all 3
        self.resnet_fc_reward_layers = []  # Define the hidden layers in the reward head of the dynamic network
        self.resnet_fc_value_layers = []  # Define the hidden layers in the value head of the prediction network
        self.resnet_fc_policy_layers = []  # Define the hidden layers in the policy head of the prediction network

        # Fully Connected Network
        #Heuristic choices!!!
        self.encoding_size = 8
        self.fc_representation_layers = []  # Define the hidden layers in the representation network
        self.fc_dynamics_layers = [16]  # Define the hidden layers in the dynamics network
        self.fc_reward_layers = [16]  # Define the hidden layers in the reward network
        self.fc_value_layers = [16]  # Define the hidden layers in the value network
        self.fc_policy_layers = [16]  # Define the hidden layers in the policy network

        ### Training
        self.results_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "../results", os.path.basename(__file__)[:-3], datetime.datetime.now().strftime("%Y-%m-%d--%H-%M-%S"))  # Path to store the model weights and TensorBoard logs
        self.save_model = True  # Save the checkpoint in results_path as model.checkpoint

        #Heuristic choice!!!
        self.training_steps = 1000000  # Total number of training steps (ie weights update according to a batch)

        # Heuristic choice!!!
        self.batch_size = 128  # Number of parts of games to train on at each training step

        self.checkpoint_interval = 10  # Number of training steps before using the model for self-playing

        #Heuristic choice!!!
        self.value_loss_weight = 1  # Scale the value loss to avoid overfitting of the value function, paper recommends 0.25 (See paper appendix Reanalyze)
        self.train_on_gpu = torch.cuda.is_available()  # Train on GPU if available

        self.optimizer = "Adam"  # "Adam" or "SGD". Paper uses SGD
        self.weight_decay = 1e-4  # L2 weights regularization
        self.momentum = 0.9  # Used only if optimizer is SGD

        # Exponential learning rate schedule
        #Heuristic choices for all 3 !!!
        self.lr_init = 0.02  # Initial learning rate
        self.lr_decay_rate = 0.9  # Set it to 1 to use a constant learning rate
        self.lr_decay_steps = 1000

        ### Replay Buffer
        #Heuristic choices for all 3 !!!
        self.replay_buffer_size = 500  # Number of self-play games to keep in the replay buffer
        self.num_unroll_steps = 10  # Number of game moves to keep for every batch element
        self.td_steps = 50  # Number of steps in the future to take into account for calculating the target value

        self.PER = True  # Prioritized Replay (See paper appendix Training), select in priority the elements in the replay buffer which are unexpected for the network
        self.PER_alpha = 0.5  # How much prioritization is used, 0 corresponding to the uniform case, paper suggests 1

        # Reanalyze (See paper appendix Reanalyse)
        self.use_last_model_value = True  # Use the last model to provide a fresher, stable n-step value (See paper appendix Reanalyze)
        self.reanalyse_on_gpu = False

        ### Adjust the self play / training ratio to avoid over/underfitting
        self.self_play_delay = 0  # Number of seconds to wait after each played game
        self.training_delay = 0  # Number of seconds to wait after each training step

        #changed from cartpole, used tictactoe!!!
        self.ratio = None  # Desired training steps per self played step ratio. Equivalent to a synchronous version, training can take much longer. Set it to None to disable it

    #Based on cartpole, possibly needs to change!!!
    def visit_softmax_temperature_fn(self, trained_steps):
        """
        Parameter to alter the visit count distribution to ensure that the action selection becomes greedier as training progresses.
        The smaller it is, the more likely the best action (ie with the highest visit count) is chosen.

        Returns:
            Positive float.
        """
        if trained_steps < 0.5 * self.training_steps:
            return 1.0
        elif trained_steps < 0.75 * self.training_steps:
            return 0.5
        else:
            return 0.25



###############################################################################
###############################  GAME  ########################################
###############################################################################
class Game(AbstractGame):
    """
    Game wrapper.
    """

    def __init__(self, seed=None, randomise=True, poss_targets=QB1GATES):
        idx = np.random.randint(0, len(poss_targets)) if randomise else 0 #by default, the first (only?) unitary
        self.env = GateSynthesis(q1_gates=QB1GATES, q2_gates=[], rwd=100, max_steps=1000,
                 init_uop=I, target_uop=poss_targets[idx], tol=1e-3)


    def step(self, action):
        """
        Apply action to the game.
        
        Args:
            action : action of the action_space to take.

        Returns:
            The new observation, the reward and a boolean if the game has ended.
        """
        observation, reward, done = self.env.step(action)
        return observation, reward, done

    def legal_actions(self):
        """
        Should return the legal actions at each turn, if it is not available, it can return
        the whole action space. At each turn, the game have to be able to handle one of returned actions.
        
        For complex game where calculating legal moves is too long, the idea is to define the legal actions
        equal to the action space but to return a negative reward if the action is illegal.        

        Returns:
            An array of integers, subset of the action space.
        """
        res = [_ for _ in range(len(self.env.full_action_list))]
        return res

    def reset(self):
        """
        Reset the game for a new game.
        
        Returns:
            Initial observation of the game.
        """
        return self.env.reset()


    def render(self):
        """
        Display the game observation.
        """
        self.env.render()
        input("Press enter to take a step ")


    def action_to_string(self, action_number):
        """
        Convert an action number to a string representing the action.

        Args:
            action_number: an integer from the action space.

        Returns:
            String representing the action.
        """
        (_ , (gate, qbits)) = self.env.full_action_list[action_number]
        return f"{action_number}. - applying {gate} on {qbits}"


###############################################################################
##########################  GATE SYNTHESIS GAME ###############################
###############################################################################
class GateSynthesis:
    def __init__(self, q1_gates=[], q2_gates=[], rwd=1000, max_steps=1000,
                 init_uop=III, target_uop=III, tol=1e-5):
        self.init_unitary_op = init_uop
        self.curr_unitary_op = self.init_unitary_op
        self.target_unitary_op = target_uop #the unitary one should generate
        self.nb_qbits = np.int(len(self.init_unitary_op.shape) / 2)
        self.final_reward = rwd
        self.q1_gates = q1_gates
        self.q2_gates = q2_gates
        self.full_action_list = self.make_full_action_list()
        self.nb_steps = 0
        self.max_steps = max_steps
        self.tol = tol
        self.distance_history = []
        self.player = 1


    def make_full_action_list(self):
        """
        Uses the one and two qubit gates allowed as well as the total number
        of qubits of the system to generate all possible combinations of
        gates on qubits.
        The result is of the for (action_index, (gate, qubit) )
        """
        q1_actions = list(itertools.product(self.q1_gates, range(self.nb_qbits)))
        if self.nb_qbits > 1:
            all_2q_permutations = list(itertools.product(range(self.nb_qbits), range(self.nb_qbits)))
            #keep only those where both qbits are different one from the other
            coherent_2q_permutations = list(filter(lambda x: x[0] != x[1], all_2q_permutations))
            q2_actions = list(itertools.product(self.q2_gates, coherent_2q_permutations))
            all_actions = q1_actions + q2_actions
        else:
            all_actions = q1_actions
        res = list(zip([_ for _ in range(len(all_actions))], all_actions)) #[(idx, (gate, qb))]
        return res


    def to_play(self):
        return 1


    def reset(self):
        self.curr_unitary_op = self.init_unitary_op
        self.nb_steps = 0
        self.distance_history = []
        return self.get_observation()


    def step(self, action_idx):
        (_ , (gate, qbit)) = self.full_action_list[action_idx]

        if (gate.shape == (2, 2, 2, 2)):  # 2qb
            (qbA, qbB) = qbit
            self.apply_2q_gate(gate, qbA, qbB)
        elif (gate.shape == (2, 2)):  # 1qb
            self.apply_1q_gate(gate, qbit)
        else:
            raise ValueError('Unsupported gate dimension')

        done = self.have_winner() or (self.nb_steps > self.max_steps)
        if done:
            print ("######### FINISH!!!")
        reward = self.final_reward if self.have_winner() else 0

        return self.get_observation(), reward, done


    def qbit_num_to_tensor_index(self, n: int):
        """Converts a qubit number in the system to its tensor index"""
        return n * 2


    def apply_1q_gate(self, gate, qbit:int):
        """Applies a 1qb gate to the current unitary and returns the resulting new unitary (and updates it)"""
        qb_idx = self.qbit_num_to_tensor_index(qbit)
        dim = self.curr_unitary_op.ndim
        lst = list(range(dim))
        tensored_res = np.tensordot(self.curr_unitary_op, gate, axes=(qb_idx, 1))
        lst.insert(qb_idx, dim - 1)
        res = np.transpose(tensored_res, lst[:-1]) #because of the way numpy handles this
        self.curr_unitary_op = res
        return res


    #index qbits as 0,1
    def apply_2q_gate(self, gate: np.array, qbitA: int, qbitB: int):
        """Applies a 2qb gate to the current unitary and returns the resulting new unitary (and updates it)"""
        idx_a = self.qbit_num_to_tensor_index(qbitA)
        idx_b = self.qbit_num_to_tensor_index(qbitB)
        dim = self.curr_unitary_op.ndim
        lst = list(range(dim))

        tensored_res = np.tensordot(self.curr_unitary_op, gate, axes=((idx_a, idx_b), (1, 3)))
        if idx_a < idx_b:
            smaller, bigger = idx_a, idx_b
            first, second = dim - 2, dim - 1
        else:
            smaller, bigger = idx_b, idx_a
            first, second = dim - 1, dim - 2
        lst.insert(smaller, first)
        lst.insert(bigger, second)
        res = np.transpose(tensored_res, lst[:-2]) #because of the way numpy handles this
        self.curr_unitary_op = res
        return res


    def get_observation(self):
        n = self.nb_qbits
        unitary = np.transpose(self.curr_unitary_op,
                               axes=tuple([el for tup in zip(range(n), range(n, 2 * n)) for el in tup]))
        unitary = unitary.reshape((2**n, 2**n))
        res = np.array([np.real(unitary), np.imag(unitary)])  # a 3D object???
        return res


    def have_winner(self):
        """Returns True if the current unitary is the target one."""
        return np.allclose(self.curr_unitary_op, self.target_unitary_op, rtol=self.tol)


    def render(self):
        print(self.curr_unitary_op)
