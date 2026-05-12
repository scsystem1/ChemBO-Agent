#!/usr/bin/env bash

# Source this script before running ChemBO experiments so compiled extensions
# resolve against the environment's C++ runtime instead of the host system one.
source /home/sunyuxiang/miniconda3/etc/profile.d/conda.sh
conda activate chembo
export LD_LIBRARY_PATH="${CONDA_PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
