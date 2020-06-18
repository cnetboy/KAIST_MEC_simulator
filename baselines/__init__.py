
from gym.envs.registration import register

register(
    id='MECS-v1',
    entry_point='baselines.environment_V_sweep:MEC_v1',
    max_episode_steps=5000,
)