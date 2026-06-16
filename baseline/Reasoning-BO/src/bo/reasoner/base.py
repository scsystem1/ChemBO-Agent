import json
from ax import Trial, Arm, GeneratorRun, Experiment
from typing import Any, Dict
import os
import re
import glob
from src.prompts.base import PromptManager
from src.utils.metric import save_trial_data

from src.utils.jsonl import add_to_jsonl, concatenate_jsonl
from src.bo.models import BOModel
from src.config import Config

config = Config()

JSON_GENERATION_MAX_TOKENS = int(os.getenv("REASONINGBO_JSON_MAX_TOKENS", "16384"))
JSON_RETRY_MAX_TOKENS = int(os.getenv("REASONINGBO_JSON_RETRY_MAX_TOKENS", "16384"))


class BaseReasoner:
    def __init__(self, exp_config_path: str, result_dir: str):
        self.exp_config = self._load_config(exp_config_path)
        self.result_dir = result_dir
        os.makedirs(result_dir, exist_ok=True)
        if not self.result_dir.endswith(('/', '\\')):
            self.result_dir += "/"

        self.trial_data_dir = self.result_dir + "trial_data/"
        self.messages_file_path = self.result_dir + "messages.jsonl"
        self.insight_history_file_path = (
            self.result_dir + "insight_history.jsonl"
        )
        self.experiment_analysis_file_path = (
            self.result_dir + "experiment_analysis.jsonl"
        )

        self.prompt_manager = PromptManager()
        self.experiment_analysis = {}
        self.overview = ""
        self.summary = ""
        self.report = ""
        self.keywords = ""
        self._latest_clean_insight = ""

    def _clean_json_response(self, raw_insight: str) -> str:
        stripped = self._strip_json_fence(raw_insight)

        try:
            parsed = json.loads(stripped)
            return self._dump_json_payload(parsed)
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
        start = stripped.find("{")
        while start != -1:
            try:
                parsed, _ = decoder.raw_decode(stripped, idx=start)
                if isinstance(parsed, dict):
                    if (
                        start != 0
                        and "hypotheses" not in parsed
                        and "parameter_sets" not in parsed
                        and self._extract_parameter_sets(parsed)
                    ):
                        start = stripped.find("{", start + 1)
                        continue
                    return self._dump_json_payload(parsed)
            except json.JSONDecodeError:
                pass
            start = stripped.find("{", start + 1)

        recovered_candidates = self._recover_parameter_sets_from_partial_json(
            stripped
        )
        if recovered_candidates:
            print(
                "Warning: Recovered candidate parameter_sets from partial LLM JSON output."
            )
            return json.dumps(
                {
                    "comment": (
                        "Recovered complete parameter_sets from a partially "
                        "truncated LLM JSON response."
                    ),
                    "keywords": "partial JSON recovery",
                    "hypotheses": [
                        {
                            "strategy": "Recovered candidate recommendations",
                            "rationale": (
                                "The original LLM response was not a complete "
                                "JSON object, but these parameter sets were "
                                "complete and parseable."
                            ),
                            "confidence": "low",
                            "parameter_sets": recovered_candidates,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        raise ValueError("Failed to extract a valid JSON object from LLM output.")

    def _strip_json_fence(self, raw_insight: str) -> str:
        stripped = (raw_insight or "").strip()
        match = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL
        )
        if match:
            return match.group(1).strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`").strip()
            if stripped.lower().startswith("json"):
                stripped = stripped[4:].strip()
        return stripped

    def _dump_json_payload(self, parsed: Any) -> str:
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
        candidates = self._extract_parameter_sets(parsed)
        if candidates:
            return json.dumps(
                {
                    "comment": "Model returned parameter sets without the expected wrapper.",
                    "keywords": "parameter sets",
                    "hypotheses": [
                        {
                            "strategy": "Direct parameter set recommendations",
                            "rationale": "Recovered from a valid JSON payload.",
                            "confidence": "low",
                            "parameter_sets": candidates,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        raise ValueError("Invalid JSON format: expected a JSON object")

    def _parameter_names(self) -> list[str]:
        names = []
        for definition in self.exp_config.get("parameter_definitions", []):
            name = definition.get("display_name")
            if name:
                names.append(str(name))
        return names

    def _extract_parameter_sets(self, payload: Any) -> list[dict[str, Any]]:
        parameter_names = self._parameter_names()
        if not parameter_names:
            return []

        candidates: list[dict[str, Any]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if all(name in value for name in parameter_names):
                    candidates.append(
                        {name: value[name] for name in parameter_names}
                    )
                    return
                for key in ("parameter_sets", "hypotheses"):
                    nested = value.get(key)
                    if isinstance(nested, list):
                        for item in nested:
                            visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(payload)

        deduped: list[dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            key = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _recover_parameter_sets_from_partial_json(
        self, text: str
    ) -> list[dict[str, Any]]:
        decoder = json.JSONDecoder()
        candidates: list[dict[str, Any]] = []
        start = text.find("{")
        while start != -1:
            try:
                parsed, _ = decoder.raw_decode(text, idx=start)
            except json.JSONDecodeError:
                start = text.find("{", start + 1)
                continue
            candidates.extend(self._extract_parameter_sets(parsed))
            start = text.find("{", start + 1)

        deduped: list[dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            key = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _generate_json_insight(
        self,
        formatted_prompt: str,
        *,
        node_name: str,
        max_tokens: int = JSON_GENERATION_MAX_TOKENS,
    ) -> str:
        raw_insight, _ = self.client.generate(
            user_prompt=formatted_prompt,
            max_tokens=max_tokens,
            json_output=True,
        )
        try:
            insight = self._clean_json_response(raw_insight)
            self._latest_clean_insight = insight
            return insight
        except Exception as first_error:
            print(
                f"Warning: {node_name} JSON parse failed once: {first_error}. "
                "Retrying with a compact JSON-only instruction."
            )

        retry_prompt = (
            formatted_prompt
            + "\n\nThe previous response was not parseable as complete JSON. "
            "Return only one compact JSON object now. Keep rationales concise. "
            "Do not include markdown. Include at least one complete parameter_sets "
            "entry using exactly the allowed parameter names and values."
        )
        raw_retry, _ = self.client.generate(
            user_prompt=retry_prompt,
            max_tokens=JSON_RETRY_MAX_TOKENS,
            json_output=True,
        )
        insight = self._clean_json_response(raw_retry)
        self._latest_clean_insight = insight
        return insight

    def _load_config(self, path: str) -> Dict:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _load_trial_data(self):
        csv_files = glob.glob(os.path.join(self.trial_data_dir, "*.csv"))
        combined_data = []
        for file_path in csv_files:
            with open(file_path, 'r', encoding='utf-8') as file:
                combined_data.append(file.read())
        return "\n".join(combined_data)

    def _save_insight(self, trial_index: int) -> None:
        new_insight = self._latest_clean_insight or self.client.messages[-1]['content']
        data = {"trial_index": trial_index, "insight": new_insight}
        add_to_jsonl(self.insight_history_file_path, data)
        self._latest_clean_insight = ""

    def _save_messages(self):
        self.client.save_messages(self.messages_file_path)

    def _extract_keywords_from_insight(self, insight: str):
        try:
            insight = insight.strip()
            insight = re.sub(
                r'^```json\s*|\s*```$', '', insight, flags=re.MULTILINE
            )
            insight_data = json.loads(insight)

            if isinstance(insight_data, dict) and "keywords" in insight_data:
                keywords = insight_data["keywords"]
                if isinstance(keywords, str):
                    return keywords.strip()
                elif isinstance(keywords, (list, tuple)):
                    return " ".join(str(k) for k in keywords).strip()

            print("Warning: No valid 'keywords' field found in insight")
            return ""

        except json.JSONDecodeError:
            print("Warning: Failed to parse insight as JSON")
            return ""
        except Exception as e:
            print(f"Warning: Unexpected error extracting keywords - {str(e)}")
            return ""

    def get_keywords(self):
        return self.keywords

    def _extract_candidates_from_insight(self, insight, n: int | None = None):
        print("Start extracting candidates array from insight...")
        insight = insight.strip()
        insight = re.sub(
            r'^```json\s*|\s*```$', '', insight, flags=re.MULTILINE
        )

        CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
        insight = json.loads(insight)
        if not isinstance(insight, dict):
            raise ValueError("Invalid JSON format: expected a JSON object")

        if "hypotheses" in insight and isinstance(insight["hypotheses"], list):
            hypotheses = insight["hypotheses"]
        elif "parameter_sets" in insight and isinstance(
            insight["parameter_sets"], list
        ):
            # Be tolerant to models that emit a single hypothesis object directly
            hypotheses = [insight]
        elif all(
            key not in insight
            for key in {
                "comment",
                "keywords",
                "strategy",
                "rationale",
                "confidence",
            }
        ):
            # Be tolerant to models that emit one bare parameter set, e.g.
            # {"C": 1.0, "gamma": 0.01}, instead of wrapping it in hypotheses.
            print("Warning: Treating bare JSON object as a single parameter set.")
            return [insight]
        else:
            raise ValueError(
                "Invalid JSON format: expected 'hypotheses' or 'parameter_sets' key"
            )

        sorted_hypotheses = sorted(
            hypotheses,
            key=lambda x: CONFIDENCE_ORDER.get(
                str(x.get("confidence", "low")).lower(), 3
            ),
        )

        candidates = []
        for hyp in sorted_hypotheses:
            if "parameter_sets" not in hyp or not isinstance(
                hyp["parameter_sets"], list
            ):
                continue

            for point in hyp["parameter_sets"]:
                if not isinstance(point, dict):
                    continue
                candidates.append(point)
                if n is not None and len(candidates) == n:
                    print(f"Done! We have collected {n} candidates.")
                    return candidates
        if n is None:
            print(f"Done! We have collected {len(candidates)} candidates.")
        else:
            print(f"Done! We have collected less than {n} candidates.")
        return candidates

    def run_bo_experiment(self, experiment, candidates_array):
        print("Start running BO experiment...")
        candidates = [Arm(parameters=params) for params in candidates_array]
        filtered_generator_run = GeneratorRun(arms=candidates)
        trial = experiment.new_batch_trial(
            generator_run=filtered_generator_run
        )
        trial.run()
        trial.mark_completed()
        print("BO experiment completed.")
        return trial

    def _save_experiment_data(self, experiment, trial: Trial) -> None:
        print("Start saving experiment data...")
        self._save_insight(trial_index=trial.index)
        self._save_messages()
        save_trial_data(
            experiment=experiment, trial=trial, save_dir=self.trial_data_dir
        )
        print("Experiment data saved.")

    def generate_overview(self) -> str:
        try:
            print("Start generating overview...")
            formatted_prompt = self.prompt_manager.format(
                "generate_overview", **self.exp_config
            )
            content, _ = self.client.generate(user_prompt=formatted_prompt)
            self.overview = content
            print(f"Overview generated:\n{content}")
            return content

        except Exception as e:
            print(f"Error generating overview: {e}")
            return ""

    def initial_sampling(self) -> str:
        try:
            print("Start initial sampling...")
            meta_dict = {**self.exp_config, "overview": self.overview}
            formatted_prompt = self.prompt_manager.format(
                "initial_sampling", **meta_dict
            )
            insight = self._generate_json_insight(
                formatted_prompt, node_name="initial_sampling"
            )
            print(f"Initial sampling process completed:\n{insight}")
            return insight

        except Exception as e:
            print(f"Error during initial sampling: {e}")
            return ""

    def optimization_first_round(self, insight, n: int | None = None):
        candidates = self._extract_candidates_from_insight(insight, n=n)
        self.keywords = self._extract_keywords_from_insight(insight)
        return candidates

    def optimization_loop(
        self,
        experiment: Experiment,
        trial: Trial,
        bo_model: BOModel,
        retrieval_context: str = None,
        n: int = 7,
    ) -> str:
        generator_run_by_bo = bo_model.gen(n=n)
        bo_candidates = [arm.parameters for arm in generator_run_by_bo.arms]

        trial_data = self._load_trial_data()

        with open(self.insight_history_file_path, 'r', encoding='utf-8') as f:
            insight_history = []
            for line_number, line in enumerate(f, 1):
                stripped_line = line.strip()
                if not stripped_line:
                    continue
                try:
                    insight_history.append(json.loads(stripped_line))
                except json.JSONDecodeError as e:
                    print(
                        f"Line {line_number} failed to parse, content: {stripped_line[:50]}..., error: {e.msg}"
                    )

        insight_history = concatenate_jsonl(insight_history)

        condidates_array = []
        try:
            print(f"Start Optimization iteration {trial.index}...")
            meta_dict = {
                **self.exp_config,
                "iteration": trial.index,
                "trial_data": trial_data,
                "insight_history": insight_history,
                "bo_recommendations": bo_candidates,
                "retrieved_context": retrieval_context,
            }
            formatted_prompt = self.prompt_manager.format(
                "optimization_loop", **meta_dict
            )
            insight = self._generate_json_insight(
                formatted_prompt, node_name=f"optimization iteration {trial.index}"
            )

            print(
                f"Optimization loop iteration {trial.index} completed:\n{insight}"
            )
            self.keywords = self._extract_keywords_from_insight(insight)
            condidates_array = self._extract_candidates_from_insight(
                insight, n=n
            )

        except Exception as e:
            print(f"Error during optimization iteration {trial.index}: {e}")
            return ""

        self._save_experiment_data(experiment=experiment, trial=trial)
        return condidates_array

    def _generate_summary(self, trial_data, insight_history):
        print("Start generating summary...")
        meta_dict = {
            **self.exp_config,
            "iteration": len(insight_history),
            "trial_data": trial_data,
            "insight_history": insight_history,
        }
        formatted_prompt = self.prompt_manager.format(
            "generate_summary", **meta_dict
        )
        insight, _ = self.client.generate(user_prompt=formatted_prompt)
        print(f"Experiment summary generated:\n{insight}")
        self.summary = insight
        self._save_messages()
        return insight

    def _generate_report(self, trial_data, insight_history):
        print("Start generating report...")
        meta_dict = {
            **self.exp_config,
            "iteration": len(insight_history),
            "trial_data": trial_data,
            "insight_history": insight_history,
        }
        formatted_prompt = self.prompt_manager.format(
            "generate_report", **meta_dict
        )
        insight, _ = self.client.generate(user_prompt=formatted_prompt)
        print(f"Experiment report generated:\n{insight}")
        self.report = insight
        self._save_messages()
        return insight

    def generate_experiment_analysis(self):
        print("Start generating experiment analysis...")
        file_path = self.result_dir + "experiment_analysis.json"
        trial_data = self._load_trial_data()
        with open(self.insight_history_file_path, 'r', encoding='utf-8') as f:
            insight_history = [json.loads(line) for line in f]

        insight_history = concatenate_jsonl(insight_history)
        analysis = {
            "overview": self.overview,
            "summary": self._generate_summary(trial_data, insight_history),
            "report": self._generate_report(trial_data, insight_history),
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(analysis, f, ensure_ascii=False, indent=4)
        print("Experiment analysis generated.")
