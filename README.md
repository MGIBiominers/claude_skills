# claude_skills

Repository to keep various Claude Code skills that people use.

A **skill** is a directory containing a `SKILL.md` (with YAML frontmatter) plus any companion files. Claude Code auto-discovers a skill when a request matches its `description`, and you can also invoke it explicitly by name (e.g. `/hpc`).

## Available skills

| Skill | What it does |
|---|---|
| [`hpc`](hpc/) | Primes a session with the WashU HPC environment (HTCF + RIS Compute2): connection, filesystems/quotas, conda/LMOD, SLURM partitions and sbatch headers, containers, monitoring, and cluster-specific failure modes. **Personalize the placeholders before first use** (see the box at the top of the SKILL). |
| [`restart`](restart/) | Gracefully prepares a session for a reboot — updates memories, records running HPC jobs, checks git status, and prints a restart checklist. |
| [`verify-references`](verify-references/) | Verifies scientific identifiers (UniProt, Pfam, DOI, PMID, NCBI accessions, etc.) against authoritative sources to catch hallucinated IDs. Ships a stdlib-only `verifier.py`. |
| [`figure-review`](figure-review/) | Reviews a scientific figure for accuracy (claims match data), aesthetics (Wilke/Tufte), and interpretability. |

## Installing a skill

Claude Code loads skills from `~/.claude/skills/`. To install one from this repo, clone it and symlink (or copy) the skill directory:

```bash
git clone https://github.com/MGIBiominers/claude_skills.git
ln -s "$PWD/claude_skills/hpc" ~/.claude/skills/hpc
# repeat for any other skill you want
```

Symlinking keeps the skill updated on `git pull`. Copying gives you a frozen, locally-editable version — better for skills like `hpc` that you personalize.

After installing, restart Claude Code (or start a new session) so it picks up the new skill. Verify with `/hpc`, `/restart`, etc.

## Personalizing `hpc`

The `hpc` skill captures **shared** WashU cluster knowledge (HTCF and RIS Compute2 are common infrastructure) with personal details replaced by placeholders: `<washu-key>`, `<lab>`, `<allocation>`, `<compute2-account>`. Fill these in for your account before relying on it. Because of that, copying rather than symlinking is recommended for `hpc`.

## Contributing a skill

1. Create a directory named after the skill, containing `SKILL.md` with frontmatter (`name`, `description`, `version`, `user-invocable`, `argument-hint`).
2. Keep it generic: no usernames, emails, SSH keys, secrets, machine-specific paths, or project-specific details. Use placeholders for anything personal.
3. Add a row to the table above.
4. Open a PR.
