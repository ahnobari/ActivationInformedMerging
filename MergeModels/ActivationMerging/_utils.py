import torch
from datasets import load_dataset
from tqdm.auto import tqdm
import numpy as np
import torch.nn as nn
from functools import partial
from ..WIDEN.utils.utils import set_random_seed, align_tokenizers_and_embeddings
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig
import logging
from copy import deepcopy


def relax_on_merged(merged_model_name, pretrained_model_name, omega = 0.2, nonlinear_scaling = lambda x: x, **kwargs):
    try:
        base_model = AutoModelForCausalLM.from_pretrained(pretrained_model_name, torch_dtype=torch.bfloat16)
        base_tokenizer = AutoTokenizer.from_pretrained(pretrained_model_name)
        base_config = AutoConfig.from_pretrained(pretrained_model_name)
    except Exception as e:
        print(f"Model {pretrained_model_name} could not be loaded.")
        print(f"Reason: {e}")
        
    try:
        merged_model = AutoModelForCausalLM.from_pretrained(merged_model_name, torch_dtype=torch.bfloat16)
        merged_tokenizer = AutoTokenizer.from_pretrained(merged_model_name)
        merged_config = AutoConfig.from_pretrained(merged_model_name)
    except Exception as e:
        print(f"Model {merged_model_name} could not be loaded.")
        print(f"Reason: {e}")
        
    logger = logging.getLogger(__name__)
    align_tokenizers_and_embeddings(pretrained_model=base_model, pretrained_tokenizer=base_tokenizer,
                                    pretrained_config=base_config, finetuned_models=[merged_model],
                                    finetuned_tokenizers=[merged_tokenizer], finetuned_configs=[merged_config], logger=logger)
    
    
    print('loading calibration dataset...')
    dataset = load_dataset("mit-han-lab/pile-val-backup", split="validation")
    dataset = dataset.shuffle(seed=42)
    
    print('getting calibration features...')
    base_model = base_model.to('cuda')
    pretrained_scale_dict = get_calib_feat(base_model, base_tokenizer, dataset)
    base_model = base_model.to('cpu')
    
    base_layer_dict = {}
    for name, param in base_model.named_modules():
        if hasattr(param, 'weight') and 'embed' not in name:
            base_layer_dict[name] = param
            
    merged_layer_dict = {}
    for name, param in merged_model.named_modules():
        if hasattr(param, 'weight') and 'embed' not in name:
            merged_layer_dict[name] = param
    
    final_weight_dict = {}
    for name, param in merged_model.named_modules():
        if hasattr(param, 'weight'):
            if 'embed' in name:
                final_weight_dict[name] = param.weight.data
            else:
                base_importance = torch.abs(pretrained_scale_dict[name])
                base_importance = base_importance / torch.max(base_importance)
                base_importance = nonlinear_scaling(base_importance)

                total_delta = merged_layer_dict[name].weight.data - base_layer_dict[name].weight.data

                relaxation_factor = 1 - (base_importance * (1 - omega))
                delta_final = total_delta * relaxation_factor

                final_weight_dict[name] = (base_layer_dict[name].weight.data + delta_final).to(torch.bfloat16)
                
    for name, param in tqdm(merged_model.named_modules(), total=sum(1 for _ in merged_model.named_modules())):
        if name in final_weight_dict:
            if torch.allclose(param.weight.data, final_weight_dict[name]):
                print(f'{name} has not changed')
            param.weight.data = final_weight_dict[name]
        elif hasattr(param, 'weight'):
            print(f'{name} not in final_weight_dict')
            
    return merged_model, merged_tokenizer

def get_calib_dataset(tokenizer=None, n_samples=256, block_size=512, dataset=None):
    if dataset is None:
        dataset = load_dataset("mit-han-lab/pile-val-backup", split="validation")
        dataset = dataset.shuffle(seed=42)
    samples = []
    n_run = 0
    for data in dataset:
        line = data["text"]
        line = line.strip()
        line_encoded = tokenizer.encode(line)
        if len(line_encoded) > block_size:
            continue
        sample = torch.tensor([line_encoded])
        if sample.numel() == 0:
            continue
        samples.append(sample)
        n_run += 1
        if n_run == n_samples:
            break

    # now concatenate all samples and split according to block size
    cat_samples = torch.cat(samples, dim=1)
    n_split = cat_samples.shape[1] // block_size
    print(f" * Split into {n_split} blocks")
    return [cat_samples[:, i*block_size:(i+1)*block_size] for i in range(n_split)]

@torch.no_grad()
def get_calib_feat(model, tokenizer, dataset=None):
    input_dict = dict()
    def stat_input_max_hook(m, x, y, name):
        if isinstance(x, tuple):
            x = x[0]
        x_max = x.view(-1, x.shape[-1]).abs().mean(dim=0).cpu().detach()
        if name not in input_dict:
            input_dict[name] = [x_max]
        else:
            input_dict[name] += [x_max]

    hooks = []
    for name, m in model.named_modules():
        if hasattr(m, "weight") and "embed" not in name:
            hooks.append(
                m.register_forward_hook(
                    partial(stat_input_max_hook, name=name)))

    device = model.device

    samples = get_calib_dataset(tokenizer, dataset=dataset)
    pbar = tqdm(samples)
    for input_ids in pbar:
        input_ids = input_ids.to(device)
        model(input_ids)

    for hook in hooks:
        hook.remove()
        
    for k, v in input_dict.items():
        input_dict[k] = sum(v).float()
        
    return input_dict

def get_models(finetuned_model_names, pretrained_model_name):
    models_to_merge, finetuned_tokenizers, finetuned_configs = [], [], []
    for finetuned_model_name in finetuned_model_names:
        try:
            finetuned_model = AutoModelForCausalLM.from_pretrained(finetuned_model_name, device_map='cpu', torch_dtype=torch.bfloat16)
            finetuned_tokenizer = AutoTokenizer.from_pretrained(finetuned_model_name)
            finetuned_config = AutoConfig.from_pretrained(finetuned_model_name)
            models_to_merge.append(finetuned_model)
            finetuned_tokenizers.append(finetuned_tokenizer)
            finetuned_configs.append(finetuned_config)
        except Exception as e:
            print(f"Model {finetuned_model_name} could not be loaded.")
            print(f"Reason: {e}")

    pretrained_model = AutoModelForCausalLM.from_pretrained(pretrained_model_name_or_path=pretrained_model_name, torch_dtype=torch.bfloat16)
    pretrained_tokenizer = AutoTokenizer.from_pretrained(pretrained_model_name_or_path=pretrained_model_name)
    pretrained_config = AutoConfig.from_pretrained(pretrained_model_name_or_path=pretrained_model_name)
    
    return models_to_merge, finetuned_tokenizers, finetuned_configs, pretrained_model, pretrained_tokenizer, pretrained_config

def get_deltas(pretrained_layer_mapping_dict, layer_mapping_dicts, pretrained_model, models_to_merge, lamb = 0.2):
    
    final_weight_dict = {}
    for name, mod in pretrained_model.named_modules():
        if hasattr(mod, 'weight') and 'embed' in name:
            final_weight_dict[name] = torch.zeros_like(mod.weight.data)
            for i in range(len(models_to_merge)):
                for name_, mod_ in models_to_merge[i].named_modules():
                    if name_ == name:
                        delta = mod_.weight.data - mod.weight.data
                        if isinstance(lamb, list):
                            delta = delta * lamb[i]
                        else:
                            delta = delta * lamb
                        final_weight_dict[name] += delta
                        
    for name, param in tqdm(pretrained_layer_mapping_dict.items(), total=len(pretrained_layer_mapping_dict)):
        final_weight_dict[name] = torch.zeros_like(param.weight.data)
        for i in range(len(models_to_merge)):
            layer_mapping_dict = layer_mapping_dicts[i]
            delta = layer_mapping_dict[name].weight.data - pretrained_layer_mapping_dict[name].weight.data
            
            if isinstance(lamb, list):
                delta = delta * lamb[i]
            else:
                delta = delta * lamb
            
            final_weight_dict[name] += delta
            
    return final_weight_dict

@torch.no_grad()
def merge(finetuned_model_names, pretrained_model_name, lamb = 0.2 , omega = 0.2, nonlinear_scaling = lambda x: x, **kwargs):

    print('loading models...')
    models_to_merge, finetuned_tokenizers, finetuned_configs, pretrained_model, pretrained_tokenizer, pretrained_config = get_models(finetuned_model_names, pretrained_model_name)
    
    logger = logging.getLogger(__name__)
    # align the tokens of pretrained and finetuned tokenizer
    align_tokenizers_and_embeddings(pretrained_model=pretrained_model, pretrained_tokenizer=pretrained_tokenizer,
                                    pretrained_config=pretrained_config, finetuned_models=models_to_merge,
                                    finetuned_tokenizers=finetuned_tokenizers, finetuned_configs=finetuned_configs, logger=logger)
    
    print('loading calibration dataset...')
    dataset = load_dataset("mit-han-lab/pile-val-backup", split="validation")
    dataset = dataset.shuffle(seed=42)
    
    print('getting calibration features...')
    pretrained_model = pretrained_model.to('cuda')
    pretrained_scale_dict = get_calib_feat(pretrained_model, pretrained_tokenizer, dataset)
    pretrained_model = pretrained_model.to('cpu')
    
    layer_mapping_dicts = []
    for i in range(len(models_to_merge)):
        layer_mapping_dict = {}
        for name, param in models_to_merge[i].named_modules():
            if hasattr(param, 'weight') and 'embed' not in name:
                layer_mapping_dict[name] = param
        layer_mapping_dicts.append(layer_mapping_dict)
    
    pretrained_layer_mapping_dict = {}
    for name, param in pretrained_model.named_modules():
        if hasattr(param, 'weight') and 'embed' not in name:
            pretrained_layer_mapping_dict[name] = param
            
    print('merging models...')
    delta_dict = get_deltas(pretrained_layer_mapping_dict, layer_mapping_dicts, pretrained_model, models_to_merge, lamb)
    
    final_weight_dict = apply_relaxation(delta_dict, pretrained_scale_dict, pretrained_layer_mapping_dict, pretrained_model, omega, nonlinear_scaling)
    
    merged_model = deepcopy(pretrained_model)
    
    for name, mod in tqdm(merged_model.named_modules(), total=sum(1 for _ in merged_model.named_modules())):
        if name in final_weight_dict:
            if torch.allclose(mod.weight.data, final_weight_dict[name]):
                print(f'{name} has not changed')
            mod.weight.data = final_weight_dict[name]
        elif hasattr(mod, 'weight'):
            print(f'{name} not in final_weight_dict')
    
    return merged_model, pretrained_tokenizer

def apply_relaxation(delta_dict, pretrained_scale_dict, pretrained_layer_mapping_dict, pretrained_model, omega = 0.2, nonlinear_scaling = lambda x: x):
    final_weight_dict = {}
    
    for name, _ in tqdm(delta_dict.items(), total=len(delta_dict)):
        if 'embed' in name:
            for name_, param in pretrained_model.named_modules():
                if name_ == name:
                    final_weight_dict[name] = (param.weight.data + delta_dict[name]).to(torch.bfloat16)
            continue
        base_importance = torch.abs(pretrained_scale_dict[name])
        base_importance = base_importance / torch.max(base_importance)
        base_importance = nonlinear_scaling(base_importance)
        
        total_delta = delta_dict[name]
        
        relaxation_factor = 1 - (base_importance * (1 - omega))
        delta_final = total_delta * relaxation_factor
        
        final_weight_dict[name] = (pretrained_layer_mapping_dict[name].weight.data + delta_final).to(torch.bfloat16)
    
    return final_weight_dict