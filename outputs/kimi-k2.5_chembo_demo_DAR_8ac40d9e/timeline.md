# ChemBO Run Timeline: `kimi-k2.5_chembo_demo_DAR_8ac40d9e`

- Started at: 2026-04-18T08:37:32.092762+00:00
- JSONL log: `outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\run_log.jsonl`
- Experiment CSV: `outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\experiment_records.csv`
- Iteration config CSV: `outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\iteration_config_records.csv`
- LLM trace: `outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\llm_trace.json`
- Final summary: `outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\final_summary.json`
- Final state: `outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\final_state.json`

## Session Start

Timestamp: 2026-04-18T08:37:32.092762+00:00
Run: `kimi-k2.5_chembo_demo_DAR_8ac40d9e`

### Summary

- Initialized campaign session.

### Outcome

- model=kimi-k2.5 | input_mode=virtual_oracle_auto | budget=40
- problem=Optimize the yield of a Direct Arylation Reaction (DAR) in a virtual chemistry space. The virtual oracle is an AutoGluon tabular regressor trained on the vendored DAR dataset. Reagent identity remains categorical, while temperature and concentration are treated as continuous variables within the experimentally observed bounds.

### Artifacts

- run_log=outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\run_log.jsonl
- timeline=outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\timeline.md
- experiment_csv=outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\experiment_records.csv
- iteration_config_csv=outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\iteration_config_records.csv
- llm_trace=outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\llm_trace.json
- final_summary=outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\final_summary.json
- final_state=outputs\kimi-k2.5_chembo_demo_DAR_8ac40d9e\final_state.json


## Step 1: `parse_input`

Timestamp: 2026-04-18T08:37:32.101776+00:00
Node: `parse_input` | Phase: `init` | Iteration: `0`

### Summary

- Parsed problem specification for DAR campaign.

### Outcome

- variables=5 | budget=40
- objective=maximize yield
- problem=Optimize the yield of a Direct Arylation Reaction (DAR) in a virtual chemistry space. The virtual oracle is an AutoGluon tabular regressor trained on the vendored DAR dataset. Reagent identity remains categorical, while temperature and concentration are treated as continuous variables within the experimentally observed bounds.


## Step 2: `select_embedding`

Timestamp: 2026-04-18T08:37:32.108285+00:00
Node: `select_embedding` | Phase: `selecting_embedding` | Iteration: `0`

### Summary

- Chose embedding `one_hot`.

### Reasoning

- Forced via settings.force_embedding_method=none.

### Outcome

- resolved=one_hot | requested=none
- dim=22 | confidence=1

### State Changes

- phase: selecting_embedding
- embedding method: one_hot


