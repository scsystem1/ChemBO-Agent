# ChemBO Run Timeline: `deepseek-reasoner_chembo_demo_DAR_346ce9c7`

- Started at: 2026-04-09T06:28:34.679525+00:00
- JSONL log: `outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\run_log.jsonl`
- Experiment CSV: `outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\experiment_records.csv`
- LLM trace: `outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\llm_trace.json`
- Final summary: `outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\final_summary.json`
- Final state: `outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\final_state.json`

## Session Start

Timestamp: 2026-04-09T06:28:34.679525+00:00
Run: `deepseek-reasoner_chembo_demo_DAR_346ce9c7`

### Summary

- Initialized campaign session.

### Outcome

- model=deepseek-reasoner | input_mode=dataset_auto | budget=10
- problem=Optimize the yield of a Direct Arylation Reaction (DAR) benchmark using the vendored DAR dataset. The search space is restricted to the experimentally observed conditions present in the CSV. During simulation, the optimizer should not see the yield column; yields are revealed only via the dataset oracle at the human-interrupt step.

### Artifacts

- run_log=outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\run_log.jsonl
- timeline=outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\timeline.md
- experiment_csv=outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\experiment_records.csv
- llm_trace=outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\llm_trace.json
- final_summary=outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\final_summary.json
- final_state=outputs\deepseek-reasoner_chembo_demo_DAR_346ce9c7\final_state.json


## Step 1: `parse_input`

Timestamp: 2026-04-09T06:28:34.685705+00:00
Node: `parse_input` | Phase: `init` | Iteration: `0`

### Summary

- Parsed problem specification for DAR campaign.

### Outcome

- variables=5 | budget=10
- objective=maximize yield
- problem=Optimize the yield of a Direct Arylation Reaction (DAR) benchmark using the vendored DAR dataset. The search space is restricted to the experimentally observed conditions present in the CSV. During simulation, the optimizer should not see the yield column; yields are revealed only via the dataset oracle at the human-interrupt step.


## Step 2: `select_embedding`

Timestamp: 2026-04-09T06:28:34.702705+00:00
Node: `select_embedding` | Phase: `selecting_embedding` | Iteration: `0`

### Summary

- Chose embedding `fingerprint_concat`.

### Reasoning

- Forced via settings.force_embedding_method=fingerprint_concat.

### Outcome

- resolved=fingerprint_concat | requested=fingerprint_concat
- dim=774 | confidence=1

### State Changes

- phase: selecting_embedding
- embedding method: fingerprint_concat


