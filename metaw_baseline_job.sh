#!/bin/bash

#SBATCH --job-name=PPO_meta_baseline
#SBATCH --output=out/ppo_metaworld_baseline_%j.out
#SBATCH --error=err/ppo_metaworld_baseline_%j.err
#SBATCH --gres=gpu:1
#SBATCH --time=20:00:00
 #SBATCH --gpus-per-task=rtx8000:1
 #SBATCH --cpus-per-task=6
 #SBATCH --ntasks-per-node=1
#SBATCH --mem=30G

# Echo time and hostname into log
echo "Date:     $(date)"
echo "Hostname: $(hostname)"

# Load any modules and activate your Python environment here
module load python/3.10 

Xvfb :1 -screen 0 1024x768x16 &
export DISPLAY=:1
export CUDA_VISIBLE_DEVICES=0 #for cuda device error

cd /home/mila/q/qingchen.hu/test_PPO/test/PPO_meta-minatar

# install or activate requirements
if ! [ -d "$SLURM_TMPDIR/env/" ]; then
    virtualenv $SLURM_TMPDIR/env/
    source $SLURM_TMPDIR/env/bin/activate
    pip install --upgrade pip
    pip install -r meta_requirements.txt
else
    source $SLURM_TMPDIR/env/bin/activate
fi

# log into WandB
export WANDB_API_KEY="5602093e351ccd9235bbc1d17997cc8c7dcacd43"
python -c "import wandb; wandb.login(key='$WANDB_API_KEY')"

# Run existing Python script in repo for tuning
python PPO_baseline.py --exp-type="ppo_metaworld" \
        --exp-name="ppo_metaworld" \
        --env-ids 'reach-v2' 'drawer-close-v2' 'window-open-v2' \
        --wandb-project-name="PPO_metaworld_baseline" \
        --rolling-window=5000 \
        --seed=2 \
        --torch-deterministic=True \
        --cuda=True \
        --track=True \
        --capture-video=False \
        --total-timesteps=1000000 \
        --learning-rate=2.5e-4 \
        --num-envs=8 \
        --num-steps=128 \