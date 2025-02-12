# Copyright 2023-present Daniel Han-Chen & the Unsloth team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__all__ = [
    "PatchFastRL",
]

import torch
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union
import inspect
import os
import re
from unsloth_zoo.compiler import create_new_function
from unsloth_zoo.logging_utils import PatchRLStatistics


def PatchRL(FastLanguageModel):

    from trl.models.utils import unwrap_model_for_generation
    from contextlib import contextmanager

    @contextmanager
    def unsloth_unwrap_model_for_generation(model, *args, **kwargs):
        with unwrap_model_for_generation(model, *args, **kwargs) as unwrapped_model:
            # Put the model in inference mode.
            FastLanguageModel.for_inference(unwrapped_model)

            # We must use .clone for Unsloth since we force inference_mode
            # Rather we should have used no_grad
            original_generate = unwrapped_model.generate
            def generate_with_clone(*args, **kwargs):
                out = original_generate(*args, **kwargs)
                if isinstance(out, torch.Tensor):
                    return out.clone()
                return out
            pass
            unwrapped_model.generate = generate_with_clone

            try:
                yield unwrapped_model
            finally:
                # Restore generate and return
                unwrapped_model.generate = original_generate
                FastLanguageModel.for_training(model)
            pass
        pass
    pass

    import trl.trainer
    trainers = dir(trl.trainer)
    trainers = [x for x in trainers if x.endswith("_trainer")]
    unwrap = "unwrap_model_for_generation"
    for trainer in trainers:
        try: current_trainer = eval(f"trl.trainer.{trainer}")
        except: continue
        if hasattr(current_trainer, unwrap):
            try: exec(f"trl.trainer.{trainer}.{unwrap} = unsloth_{unwrap}")
            except: continue
    pass
pass


# Handles NEFTune
def neftune_post_forward_hook(module, input, output):
    if module.training:
        dims = torch.tensor(output.size(1) * output.size(2))
        mag_norm = module.neftune_noise_alpha / torch.sqrt(dims)
        output = output + torch.zeros_like(output).uniform_(-mag_norm, mag_norm)
    return output
pass


RLTrainer_replacement = '''
import os
from typing import *
from dataclasses import dataclass, field
from packaging.version import Version

@dataclass
class Unsloth{RLConfig_name}({RLConfig_name}):
    """
    {__RLConfig_doc__}
    """
    sampling_params: Optional[Any] = field(
        default = None,
        metadata = {{'help': 'vLLM SamplingParams'}},
    )
    def __init__({RLConfig_arguments},
        sampling_params = None,
        **kwargs,
    ):
{RLConfig_extra_args}
        super().__init__({RLConfig_call_args}{RLConfig_kwargs})
pass

{RLTrainer_extras}

class Unsloth{RLTrainer_name}(_Unsloth{RLTrainer_name}):
    """
    {__RLTrainer_doc__}
    """
    def __init__({RLTrainer_arguments},
        **kwargs
    ):
        if args is None: args = Unsloth{RLConfig_name}()
{RLTrainer_extra_args}
        super().__init__({RLTrainer_call_args}{RLTrainer_kwargs})
{RLTrainer_post}
pass
'''

def _patch_trl_rl_trainers(trainer_file = "grpo_trainer"):
    # Patch for vLLM and Unsloth PEFT
    import trl
    import trl.trainer
    try:
        trainer = eval(f"trl.trainer.{trainer_file}")
    except Exception as error:
        return
    
    # Get SFTTrainer and SFTConfig names
    name   = [x for x in dir(trainer) if x.endswith("Trainer") and x != "Trainer" and trainer_file.split("_")[0] in x.lower()]
    config = [x for x in dir(trainer) if x.endswith("Config")  and x != "Config"  and trainer_file.split("_")[0] in x.lower()]
    if len(name)   != 1: return
    if len(config) != 1: return

    # Get SFTTrainer, SFTConfig
    RLTrainer_name = name[0]
    RLConfig_name  = config[0]
    try: RLTrainer = eval(f"trl.trainer.{trainer_file}.{RLTrainer_name}")
    except: return
    try: RLConfig  = eval(f"trl.trainer.{trainer_file}.{RLConfig_name}" )
    except: return

    # Check name
    if RLTrainer.__name__.startswith("Unsloth"): return
    if RLConfig .__name__.startswith("Unsloth"): return

    all_imports = dir(trainer)
    imports = [x for x in all_imports if not x.startswith("_")]

    # Get default arguments
    EMPTY = inspect.Parameter.empty
    processed = []
    for RLobject in [RLTrainer, RLConfig]:
        parameters = inspect.signature(RLobject.__init__).parameters
        types = (bool, type(None), int, float, str,)
        arguments = ["self"]
        call_args = []
        for k, v in parameters.items():
            if k == "self": continue
            v = v.default
            if v == "\n": v = re.escape("\n")
            if v is EMPTY: arguments.append(k)
            elif type(v) is str:   arguments.append(f"{k} = '{v}'")
            elif type(v) in types: arguments.append(f"{k} = {v}")
            else: continue
            call_args.append(f"{k} = {k}")
        pass
        arguments = f"\n{' '*8}" + f",\n{' '*8}".join(arguments)
        call_args = f"\n{' '*12}" + f",\n{' '*12}".join(call_args)
        processed.append((arguments, call_args,))
    pass

    # Process RLTrainer first
    arguments, call_args = processed[0]
    RLTrainer_post = ""

    # Add tokenizer if not seen
    if "tokenizer" not in parameters and "processing_class" in parameters:
        arguments += f",\n{' '*8}tokenizer = None"
        call_args = call_args.replace(
            "processing_class = processing_class",
            "processing_class = tokenizer if tokenizer is not None else processing_class",
        )
    pass

    # Edit bf16, fp16 by checking model's torch_dtype directly
    extra_args = ""
    if "args" in call_args and "model" in call_args:
        mixed_precision = \
        "use_bf16 = getattr(args, 'bf16', False)\n"\
        "use_fp16 = getattr(args, 'fp16', False)\n"\
        "dtype = getattr(model.config, 'torch_dtype', None)\n"\
        "if dtype is None: dtype = model.get_input_embeddings().dtype\n"\
        "from unsloth_zoo.utils import _get_dtype\n"\
        "dtype = _get_dtype(dtype)\n"\
        "float16 = dtype == torch.float16\n"\
        "if float16 and use_bf16: raise TypeError('Unsloth: Model is in float16 precision but you want to use bfloat16 precision. Set fp16 to `True` and bf16 to `False`')\n"\
        "if not float16 and use_fp16: raise TypeError('Unsloth: Model is in bfloat16 precision but you want to use float16 precision. Set fp16 to `False` and bf16 to `True`')\n"\
        "if not use_bf16 and not use_fp16:\n"\
        "    args.fp16 = float16\n"\
        "    args.bf16 = not float16\n"\
        "    os.environ['ACCELERATE_MIXED_PRECISION'] = 'fp16' if float16 else 'bf16'\n"
        extra_args += mixed_precision
    pass

    # Check if per_device_eval_batch_size (default 8) bigger than bsz
    # Also use FP16 / BF16 evaluation
    if "args" in call_args:
        # Check eval_dataset first
        if "eval_dataset" in call_args:
            check_eval_dataset = \
            "if getattr(args, 'eval_dataset', None) is not None and "\
            "getattr(args, 'eval_strategy', 'no') == 'no':\n"\
            "    args.eval_strategy = 'steps'\n"\
            "    if getattr(args, 'eval_steps', None) is None: args.eval_steps = 0.1\n"
            extra_args += check_eval_dataset
        pass

        # Check if gradient accumulation bug fix is applied
        check_ga = \
        "ga_steps = getattr(args, 'gradient_accumulation_steps', None)\n"\
        "if ga_steps is not None and ga_steps > 1:\n"\
        "    from transformers import __version__ as transformers_version\n"\
        "    if Version(transformers_version) <= Version('4.45.2'):\n"\
        "        print('**** Unsloth: Please use our fixed gradient_accumulation_steps by updating transformers, TRL and Unsloth!\\n'\n"\
        "              '`pip install --upgrade --no-cache-dir --force-reinstall --no-deps unsloth transformers trl unsloth_zoo`')\n"
        extra_args += check_ga

        eval_changes = \
        "if getattr(args, 'eval_strategy', 'no') != 'no':\n"\
        "    eval_bsz = getattr(args, 'per_device_eval_batch_size', 8)\n"\
        "    if eval_bsz == 8 and args.per_device_train_batch_size < eval_bsz: args.per_device_eval_batch_size = args.per_device_train_batch_size\n"\
        "    if getattr(args, 'eval_accumulation_steps', None) is None and ga_steps is not None: args.eval_accumulation_steps = ga_steps\n"\
        "fp16_full_eval = getattr(args, 'fp16_full_eval', False)\n"\
        "bf16_full_eval = getattr(args, 'bf16_full_eval', False)\n"\
        "if args.fp16 and bf16_full_eval: args.bf16_full_eval = False; args.fp16_full_eval = True\n"\
        "if args.bf16 and fp16_full_eval: args.bf16_full_eval = True; args.fp16_full_eval = False\n"\
        "if not bf16_full_eval and not fp16_full_eval: args.bf16_full_eval = args.bf16; args.fp16_full_eval = args.fp16\n"
        extra_args += eval_changes
    pass

    # Check max_seq_length
    if "model" in call_args:
        length_check = \
        "if 'max_seq_length' not in locals() and not hasattr(args, 'max_seq_length'):\n"\
        "    pass\n"\
        "else:\n"\
        "    model_max_seq_length = getattr(model, 'max_seq_length', None)\n"\
        "    args_max_seq_length  = getattr(args,  'max_seq_length', None)\n"\
        "    if args_max_seq_length is None and model_max_seq_length is not None:\n"\
        "        max_seq_length = model.max_seq_length\n"\
        "        if hasattr(args, 'max_seq_length'): args.max_seq_length = max_seq_length\n"
        "    elif args_max_seq_length is not None and model_max_seq_length is not None:\n"\
        "        if args_max_seq_length > model_max_seq_length:\n"\
        "            print('Unsloth: You set `max_seq_length` as ' + str(args_max_seq_length) + ' but \n"\
        "                   the maximum the model supports is ' + str(model_max_seq_length) + '. We shall reduce it.')\n"\
        "            args.max_seq_length = model_max_seq_length\n"
        extra_args += length_check
    pass

    # Enable for training and move padding side of tokenizer to right
    if "model" in call_args:
        training_check = \
        "if model is not None and hasattr(model, 'for_training'):\n"\
        "    model.for_training()\n"\
        "if 'tokenizer' in locals() and hasattr(tokenizer, 'padding_side'): tokenizer.padding_side = 'right'\n"\
        "if 'processing_class' in locals():\n"\
        "    if hasattr(processing_class, 'padding_side'): processing_class.padding_side = 'right'\n"\
        "    if hasattr(processing_class, tokenizer) and hasattr(processing_class.tokenizer, 'padding_side'): "\
        "processing_class.tokenizer.padding_side = 'right'\n"
        extra_args += training_check
    pass

    # Check NEFTune
    if "model" in call_args:
        neftune_check = \
        "if hasattr(self, 'neftune_hook_handle'):\n"\
        "    self.neftune_hook_handle.remove()\n"\
        "    if hasattr(self, 'neftune_hook_handle'): del self.neftune_hook_handle\n"\
        "if getattr(args, 'neftune_noise_alpha', None) is not None:\n"\
        "    model.get_input_embeddings().neftune_noise_alpha = self.neftune_noise_alpha\n"\
        "    self.neftune_hook_handle = self.model.get_input_embeddings().register_forward_hook(neftune_post_forward_hook)\n"\
        "pass\n"
        RLTrainer_post += neftune_check
    pass

    # Add statistics as well!
    extra_args += \
        "from unsloth_zoo.logging_utils import PatchRLStatistics\n"\
        f"PatchRLStatistics('{trainer_file}')\n"

    # Create RLTrainer args
    extra_args = extra_args.split("\n")
    extra_args = "\n".join(" "*8 + x for x in extra_args)
    RLTrainer_post = RLTrainer_post.split("\n")
    RLTrainer_post = "\n".join(" "*8 + x for x in RLTrainer_post)
    RLTrainer_arguments  = arguments
    RLTrainer_extra_args = extra_args
    RLTrainer_call_args  = call_args

    # Fix RLConfig next
    arguments, call_args = processed[1]
    extra_args = ""

    # Edit GA / bsz and weight_decay
    replacements = {
        "output_dir"                  : None,
        "logging_nan_inf_filter"      : False,
        "per_device_train_batch_size" : 4,
        "gradient_accumulation_steps" : 2,
        "weight_decay"                : 0.01,
        "warmup_ratio"                : 0.1,
        "seed"                        : 3407,
        "optim"                       : "adamw_8bit",
        "learning_rate"               : 5e-05,
        "per_device_eval_batch_size"  : 4,
        "eval_accumulation_steps"     : 2,
        "torch_empty_cache_steps"     : 250,
        "logging_steps"               : 1,
    }
    for k, v in replacements.items():
        x = f"{k}( = [^,\n]{{1,}})?,\n"
        y = f"'{v}'" if type(v) is str else f"{v}"
        y = f"{k} = {y},\n"
        arguments = re.sub(x, y, arguments)
    pass

    # Warn on too large or too small learning rate
    if " learning_rate" in call_args:
        learning_rate_check = \
        "if learning_rate < 1e-7: raise FloatingPointError(f'Unsloth: Your learning rate of `{learning_rate}` is too small and less than 1e-7! "\
        "Consider increasing it, otherwise gradient updates will be close to 0!')\n"\
        "if learning_rate > 1: raise OverflowError(f'Unsloth: Your learning rate of `{learning_rate}` is way too larger > 1! "\
        "Consider decreasing it to 1e-1, otherwise gradient updates will explode!')\n"
        extra_args += learning_rate_check
    pass

    # Add output_dir saving
    if "output_dir" in call_args:
        # Default checks
        saving_check = \
        "if output_dir is None and save_strategy == 'steps' and save_steps == 500:\n"\
        "    output_dir = 'unsloth_training_checkpoints'\n"\
        "    save_strategy = 'no'\n"
        extra_args += saving_check
    pass

    # Edit dataset_num_proc
    if "dataset_num_proc" in call_args:
        num_proc_check = \
        "if dataset_num_proc is None:\n"\
        "    from multiprocessing import cpu_count\n"\
        "    dataset_num_proc = cpu_count()\n"
        extra_args += num_proc_check
    pass

    # Edit report_to and default it to nothing if max_steps is like 60

    # Create RLConfig args
    extra_args = extra_args.split("\n")
    extra_args = "\n".join(" "*8 + x for x in extra_args)
    RLConfig_arguments  = arguments
    RLConfig_extra_args = extra_args
    RLConfig_call_args  = call_args

    # Patch vLLM
    RLTrainer_extras = patch_vllm(RLTrainer, trainer_file, RLTrainer_name, all_imports, imports)
    if RLTrainer_extras is None:
        RLTrainer_extras = f"_Unsloth{RLTrainer_name} = {RLTrainer_name}"

    # Create full module
    exec(f"from trl.trainer import ({RLTrainer_name}, {RLConfig_name},)")
    __RLTrainer_doc__ = eval(f"trl.trainer.{RLTrainer_name}").__doc__
    __RLConfig_doc__  = eval(f"trl.trainer.{RLConfig_name}") .__doc__

    RLTrainer_source = RLTrainer_replacement.format(
        RLTrainer_name       = RLTrainer_name,
        __RLTrainer_doc__    = __RLTrainer_doc__,
        RLTrainer_arguments  = RLTrainer_arguments,
        RLTrainer_extra_args = RLTrainer_extra_args,
        RLTrainer_call_args  = RLTrainer_call_args,
        RLTrainer_kwargs     = ",**kwargs"[1 if RLTrainer_call_args.endswith(",") else 0:],

        RLConfig_name        = RLConfig_name,
        __RLConfig_doc__     = __RLConfig_doc__,
        RLConfig_arguments   = RLConfig_arguments,
        RLConfig_extra_args  = RLConfig_extra_args,
        RLConfig_call_args   = RLConfig_call_args,
        RLConfig_kwargs      = ",**kwargs"[1 if RLConfig_call_args .endswith(",") else 0:],

        RLTrainer_extras     = RLTrainer_extras,
        RLTrainer_post       = RLTrainer_post,
    )

    # Create new function
    created_module = create_new_function(
        f"Unsloth{RLTrainer_name}",
        RLTrainer_source,
        f"trl.trainer.{trainer_file}",
        imports,
    )
    
    # Patch Trainer
    exec(f"trl.{RLTrainer_name} = created_module.Unsloth{RLTrainer_name}", locals(), globals())
    exec(f"trl.trainer.{RLTrainer_name} = created_module.Unsloth{RLTrainer_name}", locals(), globals())
    exec(f"trl.trainer.{trainer_file}.{RLTrainer_name} = created_module.Unsloth{RLTrainer_name}", locals(), globals())
    
    # Patch Config
    exec(f"trl.{RLConfig_name} = created_module.Unsloth{RLConfig_name}", locals(), globals())
    exec(f"trl.trainer.{RLConfig_name} = created_module.Unsloth{RLConfig_name}", locals(), globals())
    exec(f"trl.trainer.{trainer_file}.{RLConfig_name} = created_module.Unsloth{RLConfig_name}", locals(), globals())
pass


def patch_vllm(RLTrainer, trainer_file, RLTrainer_name, all_imports, imports):
    init = inspect.getsource(RLTrainer.__init__)
    old_init = init

    # Remove peft_config
    init = init.replace("elif peft_config is None:", "elif False:")
    init = init.replace("elif peft_config is not None:", "elif False:")
    init = init.replace("if peft_config is None:", "if False:")
    init = init.replace("if peft_config is not None:", "if False:")
    init = init.replace("get_peft_model(model, peft_config)", "model")

    # Set use_vllm if not set
    init = re.sub(
        r"\)([ ]{0,}\-\>[ ]{0,}None[ ]{0,}):\n([\s]{4})",
        r"):\n\2    "\
        r"if hasattr(model, 'vllm_engine') and "\
        r"getattr(args, 'use_vllm') and getattr(args, 'use_vllm', False): "\
        r"args.use_vllm = True\n\2",
        init, 1,
    )

    vllm_part = re.findall(
        r"(\n[\s]{8}"\
        r"if (self|args)\.use_vllm\:.+?"\
        r"\n[\s]{8,}"\
        "else:\n)",
        init,
        flags = re.MULTILINE | re.DOTALL,
    )
    if len(vllm_part) == 1:
        vllm_part, args = vllm_part[0][0], vllm_part[0][1]
        # Strip all comments
        new_vllm_part = re.sub(r"\#[^\n]{1,}\n", "", vllm_part)

        # Get SamplingParams
        sampling_params = re.findall(
            r"\n[\s]{4,}(self\.[^\s]{1,}[\s]{0,}\=[\s]{0,}"\
            r"SamplingParams\(.+?\))",
            new_vllm_part,
            flags = re.MULTILINE | re.DOTALL,
        )
        if len(sampling_params) == 1:
            sampling_params = sampling_params[0]
            # Replace with our vLLM engine
            sampling_params = \
                " "*12 + "self.llm = model.vllm_engine; self._last_loaded_step = 0; " + \
                sampling_params # Add spaces
            new_vllm_part = \
                f"\n{' '*8}if {args}.use_vllm:\n{sampling_params} "\
                f"if getattr(args, 'sampling_params', None) is None else "\
                f"getattr(args, 'sampling_params', None)\n{' '*8}else:\n"
            init = init.replace(vllm_part, new_vllm_part)
        pass
    pass

    # Search for vLLM calling in all child functions
    functions = dir(RLTrainer)
    RLTrainer_source = inspect.getsource(RLTrainer)
    functions = [x for x in functions if f"def {x}" in RLTrainer_source]

    changed = {"__init__" : (old_init, init,)}

    for function in functions:
        if not hasattr(RLTrainer, function): continue
        fx = getattr(RLTrainer, function)
        try: source = inspect.getsource(fx)
        except: continue
        original_source = source

        # llm_model = self.llm.llm_engine.model_executor.driver_worker.model_runner.model
        source = re.sub(
            r"(\n[\s]{4,}).+?model_executor\.driver_worker.+?\n",
            r"\n\1pass\n",
            source,
        )

        # llm_model.load_weights(model.state_dict().items())
        source = re.sub(
            r"(\n[\s]{4,}).+?load_weights\(.+?\n",
            r"\n\1pass\n",
            source,
        )

        # .state_dict()
        source = re.sub(
            r"\.state_dict\(\)",
            r"",
            source,
        )
        
        # Replace self.llm.generate and self.llm.chat
        lora_name = trainer_file + "_lora_model"
        source = re.sub(
            r"(self\.llm\.(?:generate|chat)\([^\)]{1,})\)",
            r"\1, lora_request = self.model.load_lora('" + lora_name + r"', load_tensors = True))",
            source
        )

        # Skip if no changes done
        if source == original_source: continue

        # Find all imports
        imports += [x for x in all_imports if not x.startswith("_") and x in source]

        changed[function] = (original_source, source,)
    pass

    # Import all functions
    imports = list(set(imports))

    # Patch all functions
    for function in changed:
        old, new = changed[function]
        RLTrainer_source = RLTrainer_source.replace(old, new)
    pass
    RLTrainer_source = RLTrainer_source.replace(
        f"class {RLTrainer_name}", f"class _Unsloth{RLTrainer_name}", 1
    )
    return RLTrainer_source
pass


def patch_trl_rl_trainers():
    # Patch all TRL modules if they have vLLM or PEFT
    import trl.trainer
    all_trainers = dir(trl.trainer)
    all_trainers = [x for x in all_trainers if x.islower() and x.endswith("_trainer")]
    for trainer in all_trainers:
        _patch_trl_rl_trainers(trainer)
    return
pass


def PatchFastRL(algorithm = None, FastLanguageModel = None):
    if FastLanguageModel is not None: PatchRL(FastLanguageModel)
    patch_trl_rl_trainers()
    if algorithm is not None: PatchRLStatistics(algorithm)
pass
