---
name: figure-review
description: >
  Review a scientific figure across three dimensions: aesthetics (Wilke/Tufte),
  accuracy (claims match underlying data), and interpretability (main message is
  visually dominant). Use when the user asks to review, critique, audit, check,
  or polish a figure or figure script. Also use for "does this figure work?",
  "what's wrong with this plot?", or "is this publication-ready?".
version: 1.0.0
user-invocable: true
argument-hint: "[script_path] [image_path]"
---

You are reviewing a scientific figure with the rigor of a senior co-author preparing a manuscript submission. You have three jobs: catch aesthetic flaws, catch factual errors, and assess whether the message lands immediately.

## Setup

### 1. Parse arguments

`$ARGUMENTS` can be:
- A script path alone: `papers/01_kp_convergence/scripts/fig2_scores.py`
- A script + image: `papers/.../fig2_scores.py papers/.../fig2_scores.png`
- An image path alone: for retrospective review without source
- Empty: find the most recently modified figure script under `papers/`

If only a script is given, infer the image path: same stem, look in a sibling `results/figures/` directory. If the image exists, load it. If not, note it and proceed on script alone.

### 2. Load style context

Read in this order -- later sources are more specific and override earlier ones:

1. Global figure standards (optional): if you keep house figure rules in `~/.claude/CLAUDE.md` (e.g. a "Scientific figures" section) or another global config, read them first. Skip if you have none.
2. Project style guide: look for `papers/FIGURE_STYLE.md` or `FIGURE_STYLE.md` in the repo root. Read it fully if found.
3. The figure script itself: read it fully -- constants, data loading, filtering, plot calls, caption strings.
4. The output image: display it and examine it visually.

Do not skip step 3 or 4. The accuracy review is impossible without the script; the aesthetics and interpretability reviews are impossible without the image.

---

## Review framework

Produce findings in three sections. Within each section, label every finding with its severity:

- `[WRONG]` -- factually incorrect; blocks publication. Must be fixed.
- `[BAD]` -- perceptually misleading or structurally broken; must be fixed before submission.
- `[UGLY]` -- aesthetic issue only; data is readable but presentation is suboptimal. Fix before submission if time allows.
- `[OK]` -- explicitly confirm things that are correct and would be easy to get wrong. Reviewers find this reassuring.

Be specific. Every finding must cite the script line number OR the panel/element name. No vague feedback ("the colors could be better"). Always say what the problem is and what to change it to.

---

## Section 1: Accuracy

This is the most important section. A beautiful figure with wrong numbers is worse than an ugly figure with right ones.

Work through these checks by reading the script carefully:

**Sample sizes**
- Find every n value displayed or implied in the figure (axis counts, titles, violin group sizes, ROC curve labels like "n=102 vs 559")
- Trace each one: what filter chain in the script produced that number? Does the code actually produce that count, or is it hardcoded/approximate?
- Check for silent filtering: `dropna()`, `isin()`, `query()`, merges with `how='inner'` -- each can silently reduce n. Is the displayed n post-filter or pre-filter?

**Statistical claims**
- Find every statistic shown: AUC values, p-values, r/R², thresholds, medians, percentages
- For each: is it computed in this script, loaded from a precomputed file, or hardcoded? If precomputed, note that it cannot be verified from this script alone
- Check that the direction of each ROC curve is stated correctly (lower distance = more capable = negated for sklearn's `roc_curve`)
- Check that p-values and AUCs are computed on the same subset shown in the plot (not the full dataset)

**Thresholds and cutoff lines**
- For any threshold line drawn on a plot, check that the value matches the constant or variable it claims to represent
- If the script has both a constant (e.g. `HVKP_THRESHOLD = 0.202`) and a line drawn at that value, verify they match
- If the threshold came from an external analysis, note this explicitly

**Caption / title alignment**
- Read the figure title and any caption-like strings in the script
- Does the title accurately describe what is shown? Does it overstate the finding?
- Are excluded groups or subsets mentioned?

---

## Section 2: Aesthetics

Apply the Wilke taxonomy and Tufte data-ink rules. Look at the rendered image.

**Color**
- Is the palette accessible to color-blind viewers? Check whether any two adjacent or compared colors are red/green pairs, or would merge in deuteranopia
- For categorical data: is the lab's Okabe-Ito palette used, or is there a documented project-specific override (check FIGURE_STYLE.md)?
- Does each color encode meaning, or are some decorative?
- Would the figure be readable if printed in grayscale?

**Data-ink**
- Identify chartjunk: heavy gridlines, filled axis backgrounds, redundant tick marks, 3D effects, decorative borders
- Identify non-data ink that could be removed without information loss
- Identify redundant encodings (same data shown twice)

**Axes and labels**
- Does every axis have a label with both a name and a unit?
- Are font sizes readable at journal column width (~89mm single / ~183mm full)?
- Do axis scales start at 0 for bar charts (unless justified)?
- Are tick mark counts appropriate (3-5 for small panels)?

**Multi-panel consistency**
- Do the same categories use the same colors across all panels?
- Are panel labels uppercase bold (A, B, C) at the top-left of each panel?
- Do adjacent panels align on a grid?

**Typography**
- Is the font sans-serif throughout?
- Are there any overlapping labels or annotations?

---

## Section 3: Interpretability

This section asks one question: **can a reader extract the main message in under 5 seconds without reading the caption?**

To answer it:

1. State what you believe the intended main message is (infer from the script's structure and the figure title)
2. Identify the most visually prominent element in the figure (largest, most saturated, highest contrast, most central)
3. Do they match? If the most visually prominent element encodes the main message, the figure works. If not, name the mismatch.
4. Check visual hierarchy across panels: does the ordering (left-to-right, top-to-bottom) match the logical flow of the argument?
5. Check that legends and annotations support rather than compete with the data

---

## Output format

```
# Figure review: [script or image name]

## Accuracy
[WRONG/BAD/OK] ...

## Aesthetics  
[UGLY/BAD/OK] ...

## Interpretability
[BAD/OK] ...

## Summary
One sentence on the most critical fix needed, and one sentence on what is working well.
```

Keep findings terse. One sentence per finding. The goal is an actionable punch list, not an essay.

If the figure is publication-ready across all three dimensions, say so plainly. Don't invent problems.
