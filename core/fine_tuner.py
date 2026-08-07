import json
import logging
import os
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)

STORAGE_ROOT = os.path.join(os.getcwd(), "storage")
DATASET_PATH = os.path.join(STORAGE_ROOT, "datasets", "train_dataset.jsonl")
RAW_DATASET_DIR = os.path.join(STORAGE_ROOT, "datasets", "raw")
STATUS_PATH = os.path.join(STORAGE_ROOT, "training_status.json")
LOSS_HISTORY_PATH = os.path.join(STORAGE_ROOT, "training_loss_history.json")
TRAINING_LOG_PATH = os.path.join(STORAGE_ROOT, "training.log")
SCRIPT_PATH = os.path.join(STORAGE_ROOT, "scripts", "unsloth_trainer.py")
ADAPTER_DIR = os.path.join(STORAGE_ROOT, "adapters", "temp")
PID_PATH = os.path.join(STORAGE_ROOT, "training_pid.json")

SUPPORTED_EXTENSIONS = {".csv", ".txt", ".json", ".jsonl", ".pdf"}


def _ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def estimate_vram_usage(model_name: str, batch_size: int = 2) -> dict:
    """
    Estimates the VRAM required for QLoRA fine-tuning.
    Returns a dict with estimate in GB and a safety warning.
    """
    name = model_name.lower()
    if "32b" in name:
        base_vram = 20.0
    elif "14b" in name:
        base_vram = 10.0
    elif "8b" in name or "7b" in name:
        base_vram = 6.0
    else:
        base_vram = 8.0

    required_vram = base_vram + (batch_size * 0.5)
    return {
        "required_gb": required_vram,
        "is_safe": required_vram < 15.0,  # Assumes ~16GB user GPU
    }


def estimate_training_minutes(preset: str) -> int:
    """Rough wall-clock estimate from preset (QLoRA 7/8B on a mid-range GPU)."""
    if "Fast" in preset:
        return 15
    if "Balanced" in preset:
        return 35
    return 75


def max_steps_for_preset(preset: str) -> int:
    if "Fast" in preset:
        return 60
    if "Balanced" in preset:
        return 120
    return 300


def get_dataset_path() -> str:
    return DATASET_PATH


def ensure_dataset_ready() -> tuple[bool, str, int]:
    """Returns (ok, message, entry_count)."""
    if not os.path.exists(DATASET_PATH):
        return False, "Nessun dataset trovato. Prepara i dati nel tab Data Prep.", 0
    count = 0
    try:
        with open(DATASET_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
    except OSError as e:
        return False, f"Impossibile leggere il dataset: {e}", 0
    if count == 0:
        return False, "Il dataset è vuoto. Carica file validi e rigeneralo.", 0
    return True, DATASET_PATH, count


def load_dataset_preview(limit: int = 5) -> list[dict]:
    """Load a few ChatML rows for UI preview."""
    ok, _, _ = ensure_dataset_ready()
    if not ok:
        return []
    rows: list[dict] = []
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            messages = obj.get("messages") or []
            user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
            assistant = next((m.get("content", "") for m in messages if m.get("role") == "assistant"), "")
            rows.append({"role": "user", "content": user[:200]})
            rows.append({"role": "assistant", "content": assistant[:200]})
            if len(rows) >= limit * 2:
                break
    return rows


def _write_chatml_entry(out_f, messages: list[dict]) -> None:
    json.dump({"messages": messages}, out_f, ensure_ascii=False)
    out_f.write("\n")


def _extract_pdf_text(file_path: str) -> str:
    """Extract text from PDF using pdfplumber, with pypdf fallback."""
    text_parts: list[str] = []
    try:
        import pdfplumber

        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(page_text.strip())
        if text_parts:
            return "\n\n".join(text_parts)
    except Exception as e:
        logger.warning(f"pdfplumber failed on {file_path}: {e}")

    try:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text.strip())
    except Exception as e:
        raise ValueError(f"PDF extraction failed: {e}") from e

    if not text_parts:
        raise ValueError("PDF contains no extractable text (maybe scanned/image-only).")
    return "\n\n".join(text_parts)


def _chunk_text(text: str, max_chars: int = 900) -> list[str]:
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else []

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= max_chars:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            current = para
        while len(current) > max_chars:
            chunks.append(current[:max_chars].rsplit(" ", 1)[0] or current[:max_chars])
            current = current[len(chunks[-1]):].lstrip()
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if len(c) >= 40]


def _pdf_to_entries(file_path: str) -> list[list[dict]]:
    text = _extract_pdf_text(file_path)
    entries: list[list[dict]] = []
    basename = os.path.basename(file_path)
    for chunk in _chunk_text(text):
        topic = chunk.split("\n", 1)[0][:120].strip()
        entries.append(
            [
                {
                    "role": "system",
                    "content": "You are a helpful AI assistant with knowledge from the provided documents.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Using our documentation ({basename}), provide the detailed information "
                        f"related to: {topic}"
                    ),
                },
                {"role": "assistant", "content": chunk},
            ]
        )
    if not entries:
        raise ValueError("Could not build training pairs from PDF text.")
    return entries


def prepare_chatml_dataset(file_paths: list) -> dict[str, Any]:
    """
    Converts raw CSV/TXT/JSON/JSONL/PDF files to ChatML JSONL format.

    Returns:
        dict with keys: path, total_entries, errors, processed_files
    """
    import csv

    _ensure_parent(DATASET_PATH)
    total_entries = 0
    errors: list[str] = []
    processed_files: list[str] = []

    if not file_paths:
        return {
            "path": DATASET_PATH,
            "total_entries": 0,
            "errors": ["Nessun file fornito."],
            "processed_files": [],
        }

    with open(DATASET_PATH, "w", encoding="utf-8") as out_f:
        for fp in file_paths:
            ext = os.path.splitext(fp)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                errors.append(f"{os.path.basename(fp)}: estensione non supportata ({ext}).")
                continue
            if not os.path.exists(fp):
                errors.append(f"{os.path.basename(fp)}: file non trovato.")
                continue

            file_entries = 0
            try:
                if ext == ".csv":
                    with open(fp, "r", encoding="utf-8-sig") as csv_f:
                        reader = csv.DictReader(csv_f)
                        if not reader.fieldnames:
                            raise ValueError("CSV senza intestazioni.")
                        fieldnames_lower = {name.lower(): name for name in reader.fieldnames if name}
                        prompt_col = next(
                            (fieldnames_lower[c] for c in ("prompt", "instruction") if c in fieldnames_lower),
                            None,
                        )
                        completion_col = next(
                            (
                                fieldnames_lower[c]
                                for c in ("completion", "response", "output")
                                if c in fieldnames_lower
                            ),
                            None,
                        )
                        if not prompt_col or not completion_col:
                            raise ValueError(
                                "CSV richiede colonne prompt/instruction e completion/response/output."
                            )
                        for row in reader:
                            prompt = (row.get(prompt_col) or "").strip()
                            completion = (row.get(completion_col) or "").strip()
                            if not prompt or not completion:
                                continue
                            _write_chatml_entry(
                                out_f,
                                [
                                    {"role": "system", "content": "You are a helpful AI assistant."},
                                    {"role": "user", "content": prompt},
                                    {"role": "assistant", "content": completion},
                                ],
                            )
                            file_entries += 1

                elif ext == ".txt":
                    with open(fp, "r", encoding="utf-8") as txt_f:
                        lines = [line.strip() for line in txt_f if line.strip()]
                    if len(lines) < 2:
                        raise ValueError("TXT richiede almeno 2 righe (user/assistant alternate).")
                    for i in range(0, len(lines) - 1, 2):
                        _write_chatml_entry(
                            out_f,
                            [
                                {"role": "user", "content": lines[i]},
                                {"role": "assistant", "content": lines[i + 1]},
                            ],
                        )
                        file_entries += 1
                    if len(lines) % 2 == 1:
                        errors.append(
                            f"{os.path.basename(fp)}: ultima riga senza coppia assistant, ignorata."
                        )

                elif ext in (".json", ".jsonl"):
                    with open(fp, "r", encoding="utf-8") as json_f:
                        if ext == ".jsonl":
                            for line_no, line in enumerate(json_f, start=1):
                                line = line.strip()
                                if not line:
                                    continue
                                obj = json.loads(line)
                                if not isinstance(obj, dict) or "messages" not in obj:
                                    errors.append(
                                        f"{os.path.basename(fp)} linea {line_no}: manca campo 'messages'."
                                    )
                                    continue
                                _write_chatml_entry(out_f, obj["messages"])
                                file_entries += 1
                        else:
                            data = json.load(json_f)
                            items = data if isinstance(data, list) else [data]
                            for idx, obj in enumerate(items):
                                if not isinstance(obj, dict) or "messages" not in obj:
                                    errors.append(
                                        f"{os.path.basename(fp)} item {idx}: manca campo 'messages'."
                                    )
                                    continue
                                _write_chatml_entry(out_f, obj["messages"])
                                file_entries += 1

                elif ext == ".pdf":
                    for messages in _pdf_to_entries(fp):
                        _write_chatml_entry(out_f, messages)
                        file_entries += 1

                if file_entries == 0:
                    errors.append(f"{os.path.basename(fp)}: nessuna entry valida estratta.")
                else:
                    processed_files.append(os.path.basename(fp))
                    total_entries += file_entries

            except Exception as e:
                logger.error(f"Failed to parse {fp}: {e}")
                errors.append(f"{os.path.basename(fp)}: {e}")

    logger.info(f"Prepared ChatML dataset with {total_entries} entries at {DATASET_PATH}")
    return {
        "path": DATASET_PATH,
        "total_entries": total_entries,
        "errors": errors,
        "processed_files": processed_files,
    }


def _hf_token() -> str | None:
    try:
        from core.data_manager import DataManager

        DataManager.load_env()
    except Exception:
        pass
    return os.getenv("HUGGINGFACE_TOKEN") or os.getenv("HF_TOKEN") or None


def build_unsloth_script(config: dict) -> str:
    """
    Generates the Python script for Unsloth training that will be executed as a subprocess.
    """
    base_model = config.get("model", "unsloth/llama-3-8b-Instruct-bnb-4bit")
    preset = config.get("preset", "🚀 Fast (Prototype)")
    max_steps = max_steps_for_preset(preset)
    learning_rate = 2e-4
    dataset_path = DATASET_PATH.replace("\\", "/")
    status_path = STATUS_PATH.replace("\\", "/")
    loss_path = LOSS_HISTORY_PATH.replace("\\", "/")
    adapter_dir = ADAPTER_DIR.replace("\\", "/")

    script_content = f'''import json
import os
import sys

# Prefer project venv / current interpreter dependencies; only hint if missing.
try:
    import unsloth  # noqa: F401
except ImportError:
    print("ERROR: unsloth non installato. Esegui:")
    print("  pip install unsloth torch trl peft bitsandbytes accelerate datasets transformers")
    sys.exit(1)

hf_token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
if hf_token:
    try:
        from huggingface_hub import login
        login(token=hf_token)
        print("HuggingFace login OK")
    except Exception as e:
        print(f"HuggingFace login warning: {{e}}")

from unsloth import FastLanguageModel
from datasets import load_dataset
from trl import SFTTrainer
from transformers import TrainingArguments, TrainerCallback
from unsloth.chat_templates import get_chat_template

DATASET_PATH = r"{dataset_path}"
STATUS_PATH = r"{status_path}"
LOSS_HISTORY_PATH = r"{loss_path}"
ADAPTER_DIR = r"{adapter_dir}"
MAX_STEPS = {max_steps}

def write_status(step, total_steps, loss, done=False):
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    payload = {{"step": step, "total_steps": total_steps, "loss": loss, "done": done}}
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f)

def append_loss(loss_value):
    os.makedirs(os.path.dirname(LOSS_HISTORY_PATH), exist_ok=True)
    history = []
    if os.path.exists(LOSS_HISTORY_PATH):
        try:
            with open(LOSS_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    history.append(float(loss_value))
    with open(LOSS_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f)

def run():
    print("Starting Unsloth Training with model: {base_model}")
    if not os.path.exists(DATASET_PATH):
        print(f"ERROR: dataset missing at {{DATASET_PATH}}")
        sys.exit(1)

    max_seq_length = 2048
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name = "{base_model}",
        max_seq_length = max_seq_length,
        dtype = None,
        load_in_4bit = True,
        token = hf_token,
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r = 16,
        target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                          "gate_proj", "up_proj", "down_proj"],
        lora_alpha = 16,
        lora_dropout = 0,
        bias = "none",
        use_gradient_checkpointing = "unsloth",
        random_state = 3407,
        use_rslora = False,
        loftq_config = None,
    )

    tokenizer = get_chat_template(
        tokenizer,
        chat_template = "chatml",
        mapping = {{"role": "role", "content": "content", "user": "user", "assistant": "assistant"}},
    )

    def formatting_prompts_func(examples):
        convos = examples["messages"]
        texts = [
            tokenizer.apply_chat_template(convo, tokenize=False, add_generation_prompt=False)
            for convo in convos
        ]
        return {{"text": texts}}

    dataset = load_dataset("json", data_files=DATASET_PATH, split="train")
    dataset = dataset.map(formatting_prompts_func, batched=True)

    class StatusCallback(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kwargs):
            logs = logs or {{}}
            loss = logs.get("loss")
            if loss is None and state.log_history:
                for item in reversed(state.log_history):
                    if isinstance(item, dict) and "loss" in item:
                        loss = item["loss"]
                        break
            if loss is None:
                loss = 0.0
            write_status(state.global_step, args.max_steps or MAX_STEPS, float(loss), done=False)
            if "loss" in logs:
                append_loss(logs["loss"])

        def on_step_end(self, args, state, control, **kwargs):
            loss = 0.0
            if state.log_history:
                for item in reversed(state.log_history):
                    if isinstance(item, dict) and "loss" in item:
                        loss = item["loss"]
                        break
            write_status(state.global_step, args.max_steps or MAX_STEPS, float(loss), done=False)

    os.makedirs(ADAPTER_DIR, exist_ok=True)
    write_status(0, MAX_STEPS, 0.0, done=False)

    trainer = SFTTrainer(
        model = model,
        tokenizer = tokenizer,
        train_dataset = dataset,
        dataset_text_field = "text",
        max_seq_length = max_seq_length,
        dataset_num_proc = min(os.cpu_count() or 1, 4),
        packing = False,
        args = TrainingArguments(
            per_device_train_batch_size = 2,
            gradient_accumulation_steps = 4,
            warmup_steps = 5,
            max_steps = MAX_STEPS,
            learning_rate = {learning_rate},
            fp16 = not unsloth.is_bfloat16_supported(),
            bf16 = unsloth.is_bfloat16_supported(),
            logging_steps = 1,
            optim = "adamw_8bit",
            weight_decay = 0.01,
            lr_scheduler_type = "linear",
            seed = 3407,
            output_dir = ADAPTER_DIR,
            report_to = "none",
        ),
        callbacks=[StatusCallback()],
    )

    trainer.train()
    model.save_pretrained(ADAPTER_DIR)
    tokenizer.save_pretrained(ADAPTER_DIR)

    final_loss = 0.0
    if trainer.state.log_history:
        for item in reversed(trainer.state.log_history):
            if isinstance(item, dict) and "loss" in item:
                final_loss = float(item["loss"])
                break
    write_status(MAX_STEPS, MAX_STEPS, final_loss, done=True)
    print(f"Training Complete! Adapter saved to {{ADAPTER_DIR}}")

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        write_status(0, MAX_STEPS, 0.0, done=False)
        print(f"TRAINING FAILED: {{e}}")
        raise
'''
    _ensure_parent(SCRIPT_PATH)
    with open(SCRIPT_PATH, "w", encoding="utf-8") as f:
        f.write(script_content)
    return SCRIPT_PATH


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in (result.stdout or "") and "No tasks" not in (result.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def is_training_running() -> bool:
    """True only if the recorded training PID is still alive."""
    if not os.path.exists(PID_PATH):
        return False
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = int(data.get("pid", 0))
        return _pid_alive(pid)
    except Exception:
        return False


def start_training_process(config: dict) -> dict[str, Any]:
    """
    Validates dataset, builds the script and starts the subprocess.
    Returns {ok, message, pid?}.
    """
    ok, msg, count = ensure_dataset_ready()
    if not ok:
        return {"ok": False, "message": msg, "pid": None}

    if is_training_running():
        return {"ok": False, "message": "Un training è già in corso. Attendi che finisca.", "pid": None}

    script_path = build_unsloth_script(config)
    _ensure_parent(STATUS_PATH)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "step": 0,
                "total_steps": max_steps_for_preset(config.get("preset", "")),
                "loss": 0.0,
                "done": False,
            },
            f,
        )
    with open(LOSS_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump([], f)

    env = os.environ.copy()
    token = _hf_token()
    if token:
        env["HUGGINGFACE_TOKEN"] = token
        env["HF_TOKEN"] = token

    _ensure_parent(TRAINING_LOG_PATH)
    log_file = open(TRAINING_LOG_PATH, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, script_path],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=os.getcwd(),
        )
    except Exception as e:
        log_file.close()
        return {"ok": False, "message": f"Impossibile avviare il processo: {e}", "pid": None}

    with open(PID_PATH, "w", encoding="utf-8") as f:
        json.dump({"pid": process.pid}, f)

    logger.info(
        f"Training started pid={process.pid} model={config.get('model')} entries={count}"
    )
    return {
        "ok": True,
        "message": f"Training avviato (PID {process.pid}) su {count} esempi.",
        "pid": process.pid,
    }


def read_training_status() -> dict[str, Any]:
    default = {"step": 0, "total_steps": 1, "loss": 0.0, "done": False}
    if not os.path.exists(STATUS_PATH):
        return default
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        default.update(data)
        return default
    except Exception:
        return default


def read_loss_history() -> list[float]:
    if not os.path.exists(LOSS_HISTORY_PATH):
        return []
    try:
        with open(LOSS_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [float(x) for x in data]
    except Exception:
        pass
    return []


def read_training_logs(max_chars: int = 8000) -> str:
    if not os.path.exists(TRAINING_LOG_PATH):
        return "Nessun log ancora. Avvia un training per vedere l'output."
    try:
        with open(TRAINING_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > max_chars:
            return content[-max_chars:]
        return content or "(log vuoto)"
    except Exception as e:
        return f"Errore lettura log: {e}"


def _extract_assistant_reply(decoded: str) -> str:
    """Strip ChatML (or similar) wrappers and return the assistant reply."""
    text = decoded
    markers = [
        "<|im_start|>assistant\n",
        "<|im_start|>assistant\r\n",
        "<|im_start|>assistant",
        "assistant\n",
    ]
    for marker in markers:
        if marker in text:
            text = text.split(marker)[-1]
            break
    for end in ("<|im_end|>", "<|eot_id|>", "</s>"):
        text = text.replace(end, "")
    return text.strip()


def run_inference(prompt: str, adapter_dir: str | None = None) -> str:
    """Inference with the saved LoRA adapter."""
    adapter_dir = adapter_dir or ADAPTER_DIR
    if not os.path.isdir(adapter_dir):
        return (
            "Adapter non trovato. Completa prima un training: "
            f"`{adapter_dir}` assente."
        )
    try:
        from unsloth import FastLanguageModel
        import torch

        token = _hf_token()
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=adapter_dir,
            max_seq_length=2048,
            dtype=None,
            load_in_4bit=True,
            token=token,
        )
        FastLanguageModel.for_inference(model)

        messages = [{"role": "user", "content": prompt}]
        device = "cuda" if torch.cuda.is_available() else "cpu"
        inputs = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
        ).to(device)
        outputs = model.generate(input_ids=inputs, max_new_tokens=256, use_cache=True)
        resp = tokenizer.batch_decode(outputs)
        return _extract_assistant_reply(resp[0])
    except Exception as e:
        return f"Error during inference (Unsloth installato? GPU disponibile?): {e}"


def export_to_ollama(adapter_dir: str | None, final_name: str) -> dict[str, Any]:
    """
    Export LoRA adapter to GGUF and optionally register it with local Ollama.
    """
    adapter_dir = adapter_dir or ADAPTER_DIR
    final_name = (final_name or "").strip().replace(" ", "-")
    if not final_name:
        return {"ok": False, "message": "Nome modello obbligatorio."}
    if not os.path.isdir(adapter_dir):
        return {"ok": False, "message": f"Adapter non trovato: {adapter_dir}"}

    logger.info(f"Exporting {adapter_dir} as {final_name}")
    try:
        from unsloth import FastLanguageModel

        token = _hf_token()
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=adapter_dir,
            max_seq_length=2048,
            dtype=None,
            load_in_4bit=True,
            token=token,
        )

        gguf_dir = os.path.join(STORAGE_ROOT, "models", final_name)
        os.makedirs(gguf_dir, exist_ok=True)
        model.save_pretrained_gguf(gguf_dir, tokenizer, quantization_method="q4_k_m")

        gguf_files = [
            os.path.join(gguf_dir, name)
            for name in os.listdir(gguf_dir)
            if name.lower().endswith(".gguf")
        ]
        if not gguf_files:
            return {
                "ok": False,
                "message": f"GGUF non trovato in {gguf_dir} dopo l'export.",
            }

        gguf_path = max(gguf_files, key=os.path.getmtime)
        modelfile_path = os.path.join(gguf_dir, "Modelfile")
        with open(modelfile_path, "w", encoding="utf-8") as mf:
            mf.write(f'FROM "{gguf_path}"\n')
            mf.write('TEMPLATE """{{ if .System }}<|im_start|>system\n{{ .System }}<|im_end|>\n{{ end }}')
            mf.write('{{ if .Prompt }}<|im_start|>user\n{{ .Prompt }}<|im_end|>\n{{ end }}')
            mf.write('<|im_start|>assistant\n{{ .Response }}<|im_end|>"""\n')

        ollama_msg = "GGUF salvato; Ollama non registrato (CLI non trovata o errore)."
        try:
            result = subprocess.run(
                ["ollama", "create", final_name, "-f", modelfile_path],
                capture_output=True,
                text=True,
                timeout=600,
                cwd=gguf_dir,
            )
            if result.returncode == 0:
                ollama_msg = f"Modello Ollama `{final_name}` creato. Prova: `ollama run {final_name}`"
            else:
                err = (result.stderr or result.stdout or "").strip()
                ollama_msg = (
                    f"GGUF pronto in `{gguf_dir}`. Registrazione Ollama fallita: {err}. "
                    f"Puoi creare manualmente: ollama create {final_name} -f {modelfile_path}"
                )
        except FileNotFoundError:
            ollama_msg = (
                f"GGUF pronto in `{gguf_dir}`. Installa Ollama e poi: "
                f"`ollama create {final_name} -f {modelfile_path}`"
            )
        except Exception as e:
            ollama_msg = f"GGUF pronto in `{gguf_dir}`. Ollama create errore: {e}"

        return {
            "ok": True,
            "message": ollama_msg,
            "gguf_path": gguf_path,
            "modelfile": modelfile_path,
        }
    except Exception as e:
        logger.error(f"Error exporting to Ollama: {e}")
        return {"ok": False, "message": f"Export fallito: {e}"}
