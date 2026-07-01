---
name: hpc
description: >
  Prime a session with the WashU HPC computing environment (HTCF and RIS
  Compute2) so cluster work can be managed without re-explaining the setup.
  Covers connection, filesystems and quotas, conda/LMOD software, SLURM
  partitions and sbatch headers, containers, job monitoring, and the failure
  modes specific to each cluster. Use when the user asks about submitting,
  monitoring, or debugging jobs on HTCF or RIS, installing software on the
  cluster, cluster storage, or SSH/VPN access to WashU compute.
version: 1.0.0
user-invocable: true
argument-hint: "[cluster: htcf | ris | blank for both]"
---

You are being primed with the WashU HPC computing environment (HTCF and RIS Compute2). Read all of this carefully before responding. Use it to manage cluster work without asking for details already covered here.

Cluster focus for this session: $ARGUMENTS (if blank, both clusters are relevant)

> **Personalize before first use.** This skill captures shared WashU cluster knowledge with personal details replaced by placeholders. Fill these in (or set them as you go):
> - `<washu-key>` — your WashU/cluster username (the literal `$USER` works in most shell commands)
> - `<lab>` — your lab/group storage namespace on HTCF (e.g. the directory under `/lts/`, `/ref/`, `/scratch/`)
> - `<allocation>` — your RIS storage allocation name (often your username or lab)
> - `<compute2-account>` — your RIS SLURM billing account (e.g. `compute2-<lab>`)
> - SSH host aliases (`htcf`, `ris`, `ris-3`) are suggestions — define them in `~/.ssh/config`.

---

## Identity / setup

- WashU key (username): `<washu-key>`
- Email for tickets: `<washu-key>@wustl.edu`
- SSH key: `~/.ssh/id_ed25519` (on macOS, store the passphrase in Keychain via `ssh-add --apple-use-keychain`)
- Keep your SSH config under version control and symlink it to `~/.ssh/config`; always edit the source, never the symlink target.

---

## HTCF (primary bioinformatics cluster)

**Connection**
- Suggested alias: `ssh htcf` → `login.htcf.wustl.edu`
- Username: `<washu-key>`
- Auth: SSH key only. No VPN required. No DUO.
- ControlPersist: 8h (set in `~/.ssh/config`)

**File systems** (replace `<lab>`/`<washu-key>` with yours; quotas vary by lab)

| Path | Purpose | Notes |
|---|---|---|
| `/home/<washu-key>` | Home + active projects | Ceph, large, no strict quota listed |
| `/lts/<lab>/` | Long-term storage | Lab-shared; organized into per-project subdirs |
| `/scratch/<lab>/<washu-key>/` | Fast compute scratch | BeeGFS; **per-user quota (often ~2TB hard limit)** |
| `/ref/<lab>/data/` | Shared reference databases | Read-only; e.g. alphafold, genomad, gtdb, checkv, bakta |

**SCRATCH QUOTA WARNING**: scratch is quota-limited. Always check before large jobs:
```bash
ssh htcf "beegfs-ctl --getquota --uid <washu-key>"
```

**Conda**
- No LMOD modules on HTCF -- conda only.
- Labs typically maintain a shared conda/miniforge install under `/ref/<lab>/software/`. Source it explicitly in scripts:
  `source /ref/<lab>/software/<conda-install>/etc/profile.d/conda.sh`
- A statically-linked `micromamba` in `~/bin/` (modern solver, doesn't touch base conda) is a reliable per-user fallback when the shared base is old.
- CRITICAL: `.bashrc` exits immediately in non-interactive SSH sessions (it has a `case $- in *i*)` guard). Never rely on `.bashrc` sourcing in sbatch jobs -- always source conda explicitly.

**Software installs (READ BEFORE INSTALLING ANYTHING)**

Per the HTCF docs (https://htcf.github.io/docs/software/): **never install software on the login node.** Two reasons:

1. **Policy:** "Any computational processes found running longer than 30 minutes can be terminated." In practice the OOM killer hits big conda solves well before 30 minutes -- they fail silently with a "Killed" message mid-solve.
2. **Spec:** "Do not install software while on the login node. Please build/install software from an interactive job." Memory budget: `--mem-per-cpu=4G` minimum, `10G+` for memory-hungry installs.

**Correct install pattern:**
```bash
# Option A: interactive shell, then run installs by hand
srun --partition=interactive --cpus-per-task=4 --mem-per-cpu=8G --time=2:00:00 --pty bash
# inside the session:
source /ref/<lab>/software/<conda-install>/etc/profile.d/conda.sh
mamba create -p /ref/<lab>/software/NEWENV -c bioconda -c conda-forge -y python=3.11 PACKAGE

# Option B: run a setup script via non-interactive srun
ssh htcf "srun --partition=interactive --cpus-per-task=4 --mem-per-cpu=8G --time=1:30:00 bash /path/to/setup.sh"
```

**Mamba/conda solver gotchas on HTCF:**
- Shared mamba/conda installs are often years old and struggle with modern bioconda recipes. A project-local micromamba binary sidesteps this.
- Always pin python explicitly (e.g. `python=3.11`) to avoid the solver picking ancient package builds.
- Use `-c bioconda -c conda-forge --channel-priority flexible`. **Do NOT use `--strict-channel-priority`** with bioconda + conda-forge.
- For tools with optional CUDA deps (e.g. CheckM2's tensorflow), prepend `CONDA_OVERRIDE_CUDA="11.8"` to the install command.
- Install envs serially, not in parallel -- shared cache lock contention will kill simultaneous installs.
- If an install fails partway through, always `rm -rf` the partial prefix before retrying.
- No Apptainer/Singularity on HTCF -- containers are not an option.

**SLURM partitions**

| Partition | Avail | Time limit | Use |
|---|---|---|---|
| `squash` | up | infinite | CPU jobs (similar to general) |
| `general` | up | infinite | CPU jobs (default) |
| `gpu` | up | infinite | GPU jobs (A100, V100, L40S) |
| `interactive` | up | 16:00:00 | srun interactive sessions (16h max) |

**GPU nodes**

| Node | GPUs | RAM | CPUs |
|---|---|---|---|
| n095 | 2x A100 + 2x V100 | 350GB | 24 |
| n096-n098 | 2x A100 each | 500GB | 72 total |
| n099 | 4x L40S | 750GB | 32 |

Prefer A100 for large model work (`--gres=gpu:a100:1`). L40S on n099 is often idle.

**Standard sbatch header for HTCF GPU jobs**
```bash
#!/bin/bash
#SBATCH --job-name=JOBNAME
#SBATCH --partition=gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=/scratch/<lab>/<washu-key>/logs/%j_JOBNAME.out
#SBATCH --error=/scratch/<lab>/<washu-key>/logs/%j_JOBNAME.err

source /ref/<lab>/software/<conda-install>/etc/profile.d/conda.sh
conda activate ENV_NAME
```

**Workflow pattern**
1. Write scripts locally, scp to cluster
2. `ssh htcf "sbatch /path/to/script.sh"`
3. Monitor: `ssh htcf "squeue -u <washu-key>"`
4. Logs: `/scratch/<lab>/<washu-key>/logs/`

---

## RIS Compute2 (H100 GPU cluster)

**Connection**
- Suggested alias: `ssh ris` → `c2-login-003.ris.wustl.edu` (login nodes are `c2-login-001/002/003`; availability varies, so define `ris-2`/`ris-3` aliases too)
- Username: `<washu-key>`
- Auth: SSH key + Cisco VPN required first (DUO happens at the VPN level via browser, not at SSH level).
- ControlPersist: 8h (official RIS recommendation)

**CRITICAL: SSH output capture**
RIS login nodes print a MOTD banner + LMOD module messages before any command output, causing SSH tasks to be backgrounded. Use the sentinel pattern to reliably capture output:
```bash
ssh ris-3 "your_command && echo SENTINEL" 2>&1
# then poll/wait for SENTINEL to appear in output
```

**SSH key authentication**
Once your key is installed on RIS, connections use it silently -- no password or DUO prompt. ControlPersist 8h means once the socket is established it covers 8 hours of work. On a new machine or after key rotation:
```bash
ssh-copy-id -i ~/.ssh/id_ed25519 <washu-key>@c2-login-003.ris.wustl.edu
# requires password + DUO autopush once; all future connections use the key
```

**CRITICAL: Login node memory limit**
6 GB per user per login node. This limit applies to **any** python/conda invocation -- not just model inference or heavy processing. Even `import numpy`, `conda activate`, or a short 5-line script counts against this limit.

**Failure mode:** SSH commands or interactive sessions that die uncleanly leave orphaned python/conda processes on the login node. These accumulate across sessions. When the combined memory of all orphaned processes reaches 6 GB, every new login attempt hangs silently after the MOTD banner -- PAM cannot allocate a shell. All three login nodes can be blocked simultaneously. The OOD file manager at https://c2-ood.ris.wustl.edu remains accessible during an outage for file upload/download.

**Prevention:** Only `curl`, `wget`, `rsync`, `sbatch`, `squeue`, and light shell operations are safe to run directly on the login node. All python/conda work must go through `sbatch` or `srun` -- no exceptions.

**Recovery:** You cannot self-recover. Root access is required to kill the orphaned processes. File an RIS ticket at `help@ris.wustl.edu` stating your username, which login nodes are affected, and that orphaned processes are consuming your 6 GB user memory limit. An engineer will clear them.

**Hardware**

CPU nodes (c2-node-[001-086]):
- Intel Xeon Gold 6548Y+, 64 cores/node (128 for c2-node-081/085, 256 for c2-node-086)
- 1 TB RAM per node, 3.5 TB NVMe SSD local storage
- 200 Gbit/s InfiniBand

GPU nodes (c2-gpu-[001-018]):
- Intel Xeon Gold 6448Y, 64 cores/node (48 for c2-gpu-017/018)
- NVIDIA H100 80GB HBM3, GPU driver 550.54.14, CUDA 12.4
- 1 TB RAM per node, 7 TB NVMe SSD local storage
- 200 Gbit/s InfiniBand

**File systems** (replace `<allocation>`/`<washu-key>` with yours)

| Path | Purpose | Notes |
|---|---|---|
| `/storage3/fs1/<allocation>/Active/` | All project data | Per-allocation quota; primary location |
| `/home/<washu-key>` | Home dir | 50GB hard limit -- configs and scripts only |
| `/scratch2/fs1/<allocation>/` | Scratch | Large default, **deleted after 28 days**, fastest I/O (VAST, 40GB/s read) |
| `/tmp/` | Local node temp | Fastest, cleaned up automatically after job ends |

Storage specs:
- storage3: Qumulo distributed filesystem, up to 12 GB/s transfer
- scratch2: VAST filesystem, up to 40 GB/s read / 10 GB/s write

**Storage access and AD groups**
Access is controlled by WashU Active Directory groups, not POSIX permissions:
- `storage-<allocation>` -- read-write access to the main allocation
- Project subdirectories get groups like `storage-<allocation>-projectname-rw` and `-ro`
Check ACL: `nfs4_getfacl /storage3/fs1/<allocation>/`

**Snapshot recovery (storage2/3)**
Files deleted within the last several days can be recovered from `.snapshot`:
```bash
ls /storage3/fs1/<allocation>/Active/.snapshot/   # shows dated folders
# copy from snapshot back to Active:
cp /storage3/fs1/<allocation>/Active/.snapshot/Thu-<allocation>_active/Active/lost_file.txt \
   /storage3/fs1/<allocation>/Active/
```
Note: Storage1 uses `.snapshots` (plural); Storage2/3 uses `.snapshot` (no s).
**Do NOT use `rsync -a` or `mv` between project quota directories** -- group ownership won't update, corrupting quota tracking. Use plain `cp` instead.

**Data transfer with Globus**
Storage2/3 allocations are accessible in the existing RIS Globus Collection. Mount by path `/storage3/fs1/<allocation>/Active`. Enable "Show Hidden Items" to see `.snapshot`.

**Open OnDemand**
Web-based GUI for RIS. URL: **https://c2-ood.ris.wustl.edu** (requires VPN).
Preferred by RIS for interactive use. Available apps: JupyterLab, MATLAB, RStudio, custom noVNC desktop.

JupyterLab via OOD includes the **WLM Magic Extension** -- submit SLURM jobs directly from notebook cells:
```python
%%slurm -p general-gpu --gres=gpu:H100:1 --mem=64G
# code here runs as a SLURM job
```
This is useful for interactive ML experiments without leaving the notebook.

**Conda / Python environments on RIS**
Home is only 50GB, so install conda/miniforge under your storage allocation, not home:
```bash
source /storage3/fs1/<allocation>/Active/<conda-install>/etc/profile.d/conda.sh && conda activate <env>
```
Note: `torch.cuda.is_available()` is False on login nodes -- always True on compute nodes. Pin PyTorch builds to the cluster CUDA (12.4) and let the H100 driver provide the runtime.

**SLURM partitions**

| Partition | Avail | Time limit | Use |
|---|---|---|---|
| `general-bigmem` | up | 15-00:00:00 | High-memory jobs (c2-bigmem-*); 8 TB max |
| `general-cpu` | up | 15-00:00:00 | CPU batch jobs; 15 day limit |
| `general-gpu` | up | 15-00:00:00 | GPU batch jobs (H100 80GB); 15 day limit |
| `general-interactive` | up | 5-00:00:00 | Interactive srun sessions; 1 MIG slice; 5 day limit |
| `general-preempt-cpu` | up | 15-00:00:00 | Preemptible CPU |
| `general-preempt-gpu` | up | 15-00:00:00 | Preemptible GPU |
| `general-short` | up | 30:00 | Short backfill jobs; 30 min limit |
| `condo-*` | up | 28-00:00:00 | Lab condo partitions (if your lab has one) |

Default job resources: 1 CPU, 4 GB RAM, 8 hours (always override these explicitly).

**LMOD module system**
RIS uses LMOD for software. A project conda env is usually preferred and handles CUDA without loading modules. Key commands if needed:
```bash
ml avail                      # list available modules
ml load ris                   # load RIS community software space (required for community apps)
ml load python/3.11.9         # load specific version
module --show_hidden avail    # show hidden packages and libraries
module list                   # show loaded modules
module purge                  # unload all modules
ml spider <app>               # search for any app across all spaces
```
The "The system default contains no modules" message on login is normal LMOD initialization output.
Fix occasional LMOD spider stack overflows: `echo 'export LMOD_SHORT_TIME=86400' >> ~/.bashrc`

**RIS THPC modules available via `ml` on Compute2**
After `ml load ris`: anaconda3, apptainer, cmake, cuda, gcc, gdb, git, gmake, intel-oneapi,
lsf2slurm, matlab, nextflow, openjdk, openmpi, pmix, podman, py-jupyter, py-keras,
py-tensorflow, py-torch, python, r, rclone, spack, sqlite, ucx, usage-report

**Community Spack domains (bioinformatics)**
Load domain then use `ml avail` to see packages. Run `module help <domain>` for contact info.
```bash
ml load ris && ml load cremins   # bedtools2, bowtie/2, bwa, cellpose, cellranger, igv,
                                  # meme, miniforge3, py-macs2, r-seurat, samtools
ml load ris && ml load nil       # freesurfer, fsl, mesa-glu, workbench
ml load ris && ml load artsci    # gaussian, gromacs
```

**RIS-hosted Docker images (work on Compute2 via Pyxis)**
AlphaFold, RELION, RFdiffusion, RoseTTAFold All-Atom, Parabricks, TensorFlow, MATLAB,
Nextflow, Rstudio, AFNI/TORTOISE, cisTEM, fsl6, Stata, SAS, and others.
Access via `--container-image='ghcr.io#washu-it-ris/<image>:<tag>'`

**Standard sbatch header for RIS GPU jobs**
```bash
#!/bin/bash
#SBATCH --job-name=JOBNAME
#SBATCH --account=<compute2-account>
#SBATCH --partition=general-gpu
#SBATCH --gres=gpu:H100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=HH:MM:SS
#SBATCH --output=/storage3/fs1/<allocation>/Active/<project>/logs/%j_JOBNAME.out
#SBATCH --error=/storage3/fs1/<allocation>/Active/<project>/logs/%j_JOBNAME.err

source /storage3/fs1/<allocation>/Active/<conda-install>/etc/profile.d/conda.sh
conda activate <env>

export HF_HOME=/storage3/fs1/<allocation>/Active/<project>/models   # keep model cache off 50GB home
```
Note: No `module load cuda` needed if your conda env ships PyTorch compiled for CUDA 12.4 -- the H100 driver (550.54.14) provides the runtime.

**Interactive jobs**
```bash
srun -A <compute2-account> -p general-interactive --pty /bin/bash           # bare metal shell
srun -A <compute2-account> -p general-interactive --gpus=1 --pty /bin/bash  # GPU shell
srun -A <compute2-account> -p general-interactive --mem=100G -n 16 <command>
```

**Job dependencies**
```bash
#SBATCH --dependency=afterok:1234    # run after job 1234 succeeds
#SBATCH --dependency=afterany:1234   # run after job 1234 completes (any exit)
#SBATCH --array=0-31                 # job array, $SLURM_ARRAY_TASK_ID available
```

**CPU constraints**
```bash
#SBATCH --constraint=intel           # Intel Sapphire Rapids nodes
#SBATCH --constraint=amd             # AMD Zen3 nodes
```

**Containers on Compute2 (Enroot/Pyxis)**
```bash
# Note: Docker Hub syntax uses # not / after registry
srun -p general-interactive --container-image=ubuntu --pty /bin/bash
srun -p general-interactive \
     --container-image='ghcr.io#washu-it-ris/ris-thpc:rocky9.2' \
     --container-mounts='/etc/profile.d,/etc/sysconfig/modules,/cm,/opt/thpc,/storage3/fs1/<allocation>/Active:/storage3/fs1/<allocation>/Active' \
     --pty /bin/bash

# Private registry auth -- add to ~/.config/enroot/.credentials:
# machine ghcr.io login <username> password <token>
mkdir -p ~/.config/enroot
```

**Building containers (Podman) -- must run as SLURM job**
```bash
# CRITICAL: set XDG dirs to scratch or home fills up
XDG_CONFIG_HOME=/scratch2/fs1/<allocation>/podman \
XDG_DATA_HOME=/scratch2/fs1/<allocation>/podman \
XDG_RUNTIME_DIR=/scratch2/fs1/<allocation>/podman/runtime \
srun -p general-cpu -c 8 --pty /bin/bash
# inside job:
module load ris shared && module load podman slurm
podman machine init && podman machine start
podman build -t myimage .
```

**Apptainer (run containers, not build)**
```bash
ml load ris && ml load apptainer
srun --pty -p general-interactive /bin/bash
apptainer exec docker://ubuntu:jammy date
# IMPORTANT: redirect cache away from 50GB home dir:
export APPTAINER_CACHEDIR=/scratch2/fs1/<allocation>   # add to ~/.bashrc
```

**Scratch2 quota**
```bash
df -h /scratch2/fs1/<allocation>    # check allocation quota (path may vary -- verify first use)
# WARNING: no notification when near limit -- old files silently deleted at 28 days
# Files older than 28 days deleted based on creation time with no warning
```

**Job monitoring commands**
```bash
squeue -u <washu-key>                                       # running/pending jobs
squeue --job 1234                                           # specific job
scontrol show job=1234                                      # detailed job info
sattach 1234.0                                              # peek at live stdout/stderr
sacct -j 1234 --format=JobID,JobName,MaxRSS,Elapsed,TotalCPU,State  # post-job stats
sjstat -c                                                   # pool/partition capacity
```

**Billing / usage report**
```bash
ml ris
ml usage-report
usage-report --completed_after=20260601 --completed_before=20260612
usage-report --completed_after=20260601 --summarize_only   # totals only
```
Account for billing: `<compute2-account>`

**Profiling jobs**
```bash
# Per-command resource usage (run inside job):
/usr/bin/time -v python myscript.py

# GPU monitoring (add to sbatch scripts):
monitor_gpu(){ while true; do
  echo "$(date +%H:%M:%S) $(nvidia-smi --id=$CUDA_VISIBLE_DEVICES --query-gpu=utilization.gpu,memory.used --format=csv,noheader)"
  sleep 5; done; }
monitor_gpu & GPU_MON_PID=$!
python train.py
kill $GPU_MON_PID

# GPU usage to CSV:
nvidia-smi --id=$CUDA_VISIBLE_DEVICES \
  --query-gpu=timestamp,utilization.gpu,memory.used,memory.free \
  --format=csv,nounits -l 1 > gpu_usage.csv &
```

**Workflow pattern**
1. Edit scripts locally
2. Push to GitHub, pull on RIS: `ssh ris-3 "git -C /path/to/repo fetch -q origin && git -C /path/to/repo reset -q --hard origin/main && echo OK" 2>&1`
3. Submit: `ssh ris-3 "sbatch /path/to/script.sh && echo SUBMITTED" 2>&1`
4. Monitor: `ssh ris-3 "squeue -u <washu-key> && echo OK" 2>&1`
5. Cancel: `ssh ris-3 "scancel JOBID && echo OK" 2>&1`
6. Logs: `scp ris-3:/path/to/logs/JOBID_name.out /tmp/ && cat /tmp/JOBID_name.out`

**Cloudbursting (Cluster Extension) -- installed, not yet configured**
BCM has Cluster Extension cloudbursting to AWS, Azure, and OCI. The infrastructure is installed
(`/cm/shared/docs/cm/cloudbursting-manual.pdf` exists on cluster) but cloud credentials have not
been configured. When enabled, jobs burst seamlessly through the existing SLURM scheduler, and
`cmjob` is the user-facing submission tool for cloud nodes.
Ask RIS (`help@ris.wustl.edu`) about enabling Cluster Extension for your lab. Key question: whether
storage3 is accessible from cloud nodes (requires hardware VPN to WashU network).

**BCM documentation on cluster**
All NVIDIA Base Command Manager manuals are at `/cm/shared/docs/cm/`:
`admin-manual.pdf`, `cloudbursting-manual.pdf`, `containerization-manual.pdf`,
`developer-manual.pdf`, `edge-manual.pdf`, `installation-manual.pdf`,
`machine-learning-manual.pdf`, `user-manual.pdf`
Latest online: https://docs.nvidia.com/base-command-manager

**Not deployed at RIS (despite being in BCM manuals)**
- Kubernetes (`kubectl`) -- not installed
- `cmjob` -- not installed (requires cloudbursting to be enabled first)
- Singularity -- use Apptainer instead (installed via `ml load ris && ml load apptainer`)

---

## General rules

- Never commit secrets, API keys, or credentials to git. Keep them out of repos and out of this skill.
- Never store data or model weights in home directories (50GB limit on RIS; Ceph shared on HTCF)
- Always source conda explicitly in sbatch scripts -- never rely on .bashrc
- Set `HF_HOME` in sbatch scripts to avoid re-downloading model weights to home cache
- Ask before `git push`, force-push, or any destructive remote operation
- Never run compute on RIS login nodes -- 6GB memory limit will kill processes
