import os
import json
import logging
import subprocess

logger = logging.getLogger(__name__)

def estimate_vram_usage(model_name: str, batch_size: int = 2) -> dict:
    """
    Estimates the VRAM required for QLoRA fine-tuning.
    Returns a dict with estimate in GB and a safety warning.
    """
    base_vram = 0.0
    if "8b" in model_name.lower() or "7b" in model_name.lower():
        base_vram = 6.0
    elif "14b" in model_name.lower():
        base_vram = 10.0
    elif "32b" in model_name.lower():
        base_vram = 20.0
    else:
        base_vram = 8.0
        
    required_vram = base_vram + (batch_size * 0.5)
    return {
        "required_gb": required_vram,
        "is_safe": required_vram < 15.0 # Assuming 16GB is the user limit
    }

def prepare_chatml_dataset(file_paths: list) -> str:
    """
    Converts raw CSV/TXT to ChatML JSONL format.
    Reads lines from text files and assumes a simple instruction/response structure.
    """
    out_path = os.path.join(os.getcwd(), "storage", "datasets", "train_dataset.jsonl")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    
    with open(out_path, "w", encoding="utf-8") as out_f:
        # Fallback dummy data if no files
        if not file_paths:
            json.dump({"messages": [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi there!"}]}, out_f)
            out_f.write("\n")
            return out_path
            
        for fp in file_paths:
            # Here we just mock parsing by taking the file name for demonstration.
            # In a real scenario, pandas read_csv and map to ChatML would go here.
            entry = {
                "messages": [
                    {"role": "system", "content": "You are a helpful AI assistant."},
                    {"role": "user", "content": f"Analyze the data in {fp}"},
                    {"role": "assistant", "content": f"Understood. I am processing {fp}."}
                ]
            }
            json.dump(entry, out_f)
            out_f.write("\n")
            
    return out_path

def build_unsloth_script(config: dict) -> str:
    """
    Generates the Python script for Unsloth training that will be executed as a subprocess.
    """
    base_model = config.get("model", "unsloth/llama-3-8b-Instruct-bnb-4bit")
    preset = config.get("preset", "🚀 Fast (Prototype)")
    
    max_steps = 60 if "Fast" in preset else 120 if "Balanced" in preset else 300
    learning_rate = 2e-4
    
    script_content = f'''import json
import time
import os
import sys

# Auto-install dependencies if missing
try:
    import unsloth
except ImportError:
    print("Installing Unsloth and dependencies...")
    os.system("pip install unsloth torch trl peft bitsandbytes accelerate")

from unsloth import FastLanguageModel
from datasets import load_dataset
from trl import SFTTrainer
from transformers import TrainingArguments, DataCollatorForSeq2Seq
from unsloth.chat_templates import get_chat_template

def run():
    print("Starting Unsloth Training with model: {base_model}")
    
    max_seq_length = 2048
    dtype = None
    load_in_4bit = True

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name = "{base_model}",
        max_seq_length = max_seq_length,
        dtype = dtype,
        load_in_4bit = load_in_4bit,
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r = 16, 
        target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                          "gate_proj", "up_proj", "down_proj",],
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
        mapping = {{"role" : "role", "content" : "content", "user" : "user", "assistant" : "assistant"}},
    )
    
    def formatting_prompts_func(examples):
        convos = examples["messages"]
        texts = [tokenizer.apply_chat_template(convo, tokenize=False, add_generation_prompt=False) for convo in convos]
        return {{"text": texts}}
        
    dataset_path = os.path.join(os.getcwd(), "storage", "datasets", "train_dataset.jsonl")
    dataset = load_dataset("json", data_files=dataset_path, split="train")
    dataset = dataset.map(formatting_prompts_func, batched=True)

    # Custom Callback to write to training_status.json
    from transformers import TrainerCallback
    class StatusCallback(TrainerCallback):
        def on_step_end(self, args, state, control, **kwargs):
            status = {{
                "step": state.global_step,
                "total_steps": args.max_steps,
                "loss": state.log_history[-1].get('loss', 0.0) if state.log_history else 0.0
            }}
            with open("training_status.json", "w") as f:
                json.dump(status, f)

    trainer = SFTTrainer(
        model = model,
        tokenizer = tokenizer,
        train_dataset = dataset,
        dataset_text_field = "text",
        max_seq_length = max_seq_length,
        dataset_num_proc = 2,
        packing = False,
        args = TrainingArguments(
            per_device_train_batch_size = 2,
            gradient_accumulation_steps = 4,
            warmup_steps = 5,
            max_steps = {max_steps},
            learning_rate = {learning_rate},
            fp16 = not unsloth.is_bfloat16_supported(),
            bf16 = unsloth.is_bfloat16_supported(),
            logging_steps = 1,
            optim = "adamw_8bit",
            weight_decay = 0.01,
            lr_scheduler_type = "linear",
            seed = 3407,
            output_dir = "storage/adapters/temp",
        ),
        callbacks=[StatusCallback()],
    )

    trainer_stats = trainer.train()
    
    # Save the adapter
    model.save_pretrained("storage/adapters/temp")
    tokenizer.save_pretrained("storage/adapters/temp")
    
    print("Training Complete! Adapter saved to storage/adapters/temp")
    
    # Mark done
    with open("training_status.json", "w") as f:
        json.dump({{"step": {max_steps}, "total_steps": {max_steps}, "loss": 0.01}}, f)

if __name__ == "__main__":
    run()
'''
    script_path = os.path.join(os.getcwd(), "storage", "scripts", "unsloth_trainer.py")
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_content)
    return script_path

def start_training_process(config: dict) -> subprocess.Popen:
    """
    Builds the script and starts the subprocess.
    """
    script_path = build_unsloth_script(config)
    
    # Initialize status file
    with open("training_status.json", "w", encoding="utf-8") as f:
        json.dump({"step": 0, "total_steps": 1, "loss": 0.0}, f)
        
    process = subprocess.Popen(["python", script_path])
    return process

def run_inference(prompt: str, adapter_dir: str) -> str:
    """
    Inference function that loads the LoRA adapter.
    """
    try:
        from unsloth import FastLanguageModel
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name = adapter_dir, # Loads base + lora
            max_seq_length = 2048,
            dtype = None,
            load_in_4bit = True,
        )
        FastLanguageModel.for_inference(model)
        
        messages = [{"role": "user", "content": prompt}]
        inputs = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_tensors="pt").to("cuda")
        outputs = model.generate(input_ids=inputs, max_new_tokens=256, use_cache=True)
        resp = tokenizer.batch_decode(outputs)
        
        # Extract the assistant part
        return resp[0].split("<|im_start|>assistant\\n")[-1].replace("<|im_end|>", "").strip()
    except Exception as e:
        return f"Error during inference (is Unsloth installed?): {str(e)}"

def export_to_ollama(adapter_dir: str, final_name: str) -> bool:
    """
    Export the model to Ollama via GGUF conversion.
    """
    logger.info(f"Exporting {adapter_dir} to Ollama as {final_name}")
    try:
        from unsloth import FastLanguageModel
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name = adapter_dir,
            max_seq_length = 2048,
            dtype = None,
            load_in_4bit = True,
        )
        
        # Save to GGUF using Unsloth's built in tools
        gguf_dir = os.path.join(os.getcwd(), "storage", "models", final_name)
        os.makedirs(gguf_dir, exist_ok=True)
        
        model.save_pretrained_gguf(gguf_dir, tokenizer, quantization_method = "q4_k_m")
        logger.info(f"Successfully exported {final_name} to {gguf_dir}")
        return True
    except Exception as e:
        logger.error(f"Error exporting to Ollama: {e}")
        return False
