import argparse
import copy
import os
import sys
import json
import base64

import cv2
import numpy as np
import torch
import torch.nn.functional as F
import subprocess
import random
import pickle as pkl
import transformers
import tokenizers

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from torch.utils.data import Dataset

import deepspeed
from functools import partial
from easydict import EasyDict as edict
from typing import Dict, Optional, Sequence, List

from PIL import Image
import time
from torch.utils.tensorboard import SummaryWriter
import shutil
import yaml

from llava.constants import DEFAULT_IMAGE_TOKEN
from llava import conversation as conversation_lib
from llava.model import *
from llava.mm_utils import tokenizer_image_token
from llava.json_fixer import repair_json
from llava.prompts_utils import get_gpt4o_edit_prompt, get_text_labels_foredit
from llava.train.train_garmentcode_outfit import ModelArguments, DataArguments, TrainingArguments, rank0_print
from llava.garment_utils_v2 import try_generate_garments, all_float_paths, get_simplified_json_config

from openai import OpenAI


os.environ["MASTER_PORT"] = "23499"


def find_all_linear_names(model, lora_target_modules=['q_proj', 'v_proj']):
    cls = torch.nn.Linear
    lora_module_names = set()
    for name, module in model.named_modules():
        if (
            isinstance(module, cls)
            and all(
                [
                    x not in name
                    for x in [
                        'mm_projector', 'vision_tower', 'vision_resampler', 'float_layer'
                    ]
                ]
            )
            and any([x in name for x in lora_target_modules])
        ):
            lora_module_names.add(name)
    return sorted(list(lora_module_names))



class LazyUnSupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(self, data_path: str,
                 tokenizer: transformers.PreTrainedTokenizer,
                 data_args: DataArguments,
                 max_len=-1):
        super(LazyUnSupervisedDataset, self).__init__()
        list_data_dict = json.load(open(data_path, "r"))

        if max_len > 0:
            list_data_dict = list_data_dict[:max_len]

        rank0_print("Formatting inputs...Skip in lazy mode")
        self.tokenizer = tokenizer
        self.list_data_dict = list_data_dict
        self.data_args = data_args

    def __len__(self):
        return len(self.list_data_dict)

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        sources = self.list_data_dict[i]

        if True:
            image_file = "docs/images/black_img.jpg"
            processor = self.data_args.image_processor
            image = Image.open(image_file).convert('RGB')
            if self.data_args.image_aspect_ratio == 'pad':
                def expand2square(pil_img, background_color):
                    width, height = pil_img.size
                    if width == height:
                        return pil_img
                    elif width > height:
                        result = Image.new(pil_img.mode, (width, width), background_color)
                        result.paste(pil_img, (0, (width - height) // 2))
                        return result
                    else:
                        result = Image.new(pil_img.mode, (height, height), background_color)
                        result.paste(pil_img, ((height - width) // 2, 0))
                        return result
                image = expand2square(image, tuple(int(x*255) for x in processor.image_mean))
                image = processor.preprocess(image, return_tensors='pt')['pixel_values'][0]
            else:
                image = processor.preprocess(image, return_tensors='pt')['pixel_values'][0]
        
        data_dict = {}
        data_dict['image_trivial'] = image
        
        data_dict['id'] = sources['id']
        data_dict['original_image_path'] = sources['image']
        data_dict['prompt'] = sources["prompt"]
        data_dict['garment_name'] = sources['garmenttype']
        data_dict['json_path'] = sources['json_path']
        return data_dict




def translate_args(model_args, data_args, training_args):
    args = edict(
        local_rank=local_rank,
        version=None,
        vis_save_path="./vis_output",
        precision="bf16",
        image_size=None,
        model_max_length=training_args.model_max_length,
        lora_r=training_args.lora_r,
        lora_alpha=training_args.lora_alpha,
        lora_dropout=training_args.lora_dropout,
        lora_target_modules=None,
        vision_tower=model_args.vision_tower,
        load_in_8bit=False,
        load_in_4bit=False,
        dataset=None,
        sample_rates=None,
        log_base_dir='./runs',
        exp_name="try_lr1e_4_generator_wildimg",
        epochs=40,
        steps_per_epoch=500,
        batch_size=4,
        grad_accumulation_steps=8,
        val_batch_size=1,
        workers=4,
        lr=1e-4,
        ce_loss_weight=1.0,
        no_eval=False,
        eval_only=False,
        vision_pretrained=None,
        resume="",
        start_epoch=0,
        print_freq=1,
        gradient_checkpointing=training_args.gradient_checkpointing,
        beta1=0.9,
        beta2=0.999,

        use_mm_start_end=False
    )   

    return args

def encode_image(image_path):
  with open(image_path, "rb") as image_file:
    return base64.b64encode(image_file.read()).decode('utf-8')



def ask_gpt4o(garment_name, garment_prompt, image_path, client):
    base64_image = encode_image(image_path)
    prompt = get_gpt4o_edit_prompt(
        garment_name, garment_prompt
    )

    response = client.chat.completions.create(
        model="gpt-4o-2024-05-13",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {   
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                            "detail": "low"
                        }
                    },
                ],
            }
        ],
        max_tokens=300,
    )

    result = response.choices[0].message.content
    result = str(result)
    print('result', result)
    result_dict = get_text_labels_foredit(result)
        
    text_description = json.dumps(result_dict)
    text_description = text_description.replace('"', "'")
    return text_description

    
def main(args):
    attn_implementation = 'flash_attention_2'
    global local_rank

    parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    local_rank = training_args.local_rank
    compute_dtype = (torch.float16 if training_args.fp16 else (torch.bfloat16 if training_args.bf16 else torch.float32))

    args = translate_args(model_args, data_args, training_args)
    args.log_dir = os.path.join(args.log_base_dir, args.exp_name)
    world_size = torch.cuda.device_count()
    args.distributed = world_size > 1

    bnb_model_from_pretrained_args = {}
    writer = None
    if local_rank == 0:
        os.makedirs(args.log_dir, exist_ok=True)

    assert training_args.bits not in [4, 8]
    assert model_args.vision_tower is not None
    assert 'mpt' not in model_args.model_name_or_path

    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=training_args.cache_dir,
        model_max_length=training_args.model_max_length,
        padding_side="right",
        use_fast=False,
    )
    tokenizer.pad_token = tokenizer.unk_token
    num_added_tokens = tokenizer.add_tokens("[SEG]")
    args.seg_token_idx = tokenizer("[SEG]", add_special_tokens=False).input_ids[-1]

    model = GarmentGPTFloat50ForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=training_args.cache_dir,
        attn_implementation=attn_implementation,
        torch_dtype=(torch.bfloat16 if training_args.bf16 else None),
        seg_token_idx=args.seg_token_idx,
        **bnb_model_from_pretrained_args
    )
    
    model.config.eos_token_id = tokenizer.eos_token_id
    model.config.bos_token_id = tokenizer.bos_token_id
    model.config.pad_token_id = tokenizer.pad_token_id

    model.config.use_cache = False ####################### ?
    assert not model_args.freeze_backbone

    assert training_args.gradient_checkpointing
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()
        model.gradient_checkpointing_enable() ###################### ？
    else:
        def make_inputs_require_grad(module, input, output):
            output.requires_grad_(True)
        model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)


    # tokenizer.add_tokens('<FLOAT>', special_tokens=True)
    assert model_args.version == "v1"
    tokenizer.pad_token = tokenizer.unk_token
    if model_args.version in conversation_lib.conv_templates: # conv_vicuna_v1
        conversation_lib.default_conversation = conversation_lib.conv_templates[model_args.version]
    else:
        conversation_lib.default_conversation = conversation_lib.conv_templates["vicuna_v1"]

    
    model.get_model().initialize_vision_modules(
        model_args=model_args,
        fsdp=training_args.fsdp
    )
    
    vision_tower = model.get_vision_tower()
    vision_tower.to(dtype=torch.bfloat16 if training_args.bf16 else torch.float16, device=training_args.device)

    for p in vision_tower.parameters():
        p.requires_grad = False
    for p in model.get_model().mm_projector.parameters():
        p.requires_grad = False

    from peft import LoraConfig, get_peft_model
    lora_config = LoraConfig(
        r=training_args.lora_r,
        lora_alpha=training_args.lora_alpha,
        target_modules=find_all_linear_names(model),
        lora_dropout=training_args.lora_dropout,
        bias=training_args.lora_bias,
        task_type="CAUSAL_LM",
    )
    if training_args.bits == 16:
        if training_args.bf16:
            model.to(torch.bfloat16)
        if training_args.fp16:
            model.to(torch.float16)
    rank0_print("Adding LoRA adapters...")
    model = get_peft_model(model, lora_config)

    model.resize_token_embeddings(len(tokenizer))

    data_args.image_processor = vision_tower.image_processor
    data_args.is_multimodal = True

    model.config.image_aspect_ratio = data_args.image_aspect_ratio
    model.config.tokenizer_padding_side = tokenizer.padding_side
    model.config.tokenizer_model_max_length = tokenizer.model_max_length

    model.config.tune_mm_mlp_adapter = training_args.tune_mm_mlp_adapter = model_args.tune_mm_mlp_adapter
    assert not model_args.tune_mm_mlp_adapter
    
    assert not training_args.freeze_mm_mlp_adapter
    model.config.freeze_mm_mlp_adapter = training_args.freeze_mm_mlp_adapter

    model.config.mm_use_im_start_end = data_args.mm_use_im_start_end = model_args.mm_use_im_start_end
    model.config.mm_projector_lr = training_args.mm_projector_lr
    training_args.use_im_start_end = model_args.mm_use_im_start_end
    model.config.mm_use_im_patch_token = model_args.mm_use_im_patch_token
    model.initialize_vision_tokenizer(model_args, tokenizer=tokenizer) ############### ?
    
    assert args.precision == "bf16"
    model = model.bfloat16().cuda()
    
    val_dataset = LazyUnSupervisedDataset(
        tokenizer=tokenizer,
        data_path=data_args.data_path_eval,
        data_args=data_args,
    )

    # ########################################################################################
    resume_path = 'checkpoints/try_7b_lr1e_4_v3_garmentcontrol_4h100_v4_final/pytorch_model.bin'
    state_dict = torch.load(resume_path, map_location="cpu")
    model.load_state_dict(state_dict, strict=True)
    model = model.bfloat16().cuda()
    device = model.device

    if data_args.data_path_eval[-1] == '/':
        data_args.data_path_eval = data_args.data_path_eval[:-1]
    dataset_name = data_args.data_path_eval.split('/')[-2]
    args.exp_name = resume_path.split('/')[-2]
    parent_folder = os.path.join(args.log_base_dir, args.exp_name, f'{dataset_name}_edit_demo')
    if not os.path.exists(parent_folder):
        os.makedirs(parent_folder)

    print('val_dataset', len(val_dataset))
    client = OpenAI()

    random.seed(0)
    all_output_dir = []
    all_json_spec_files = []
    for i in range(len(val_dataset)):    
        data_item = val_dataset[i]      

        gpt_4o_description = ask_gpt4o(
            data_item['garment_name'], data_item['prompt'], data_item['original_image_path'], client)

        answers = []
        old_sewing_pattern_json = get_simplified_json_config(data_item['json_path'])
        question2 = prompt = f"Adjust the old sewing pattern according to the text descriptions.\nThe old garment sewing pattern is: \n{old_sewing_pattern_json}. \nAnd the text descriptions are: \n{gpt_4o_description}."
        
        questions = [question2]
        
        for k in range(len(questions)):
            conv = conversation_lib.conv_templates[model_args.version].copy()
            conv.messages = []
           
            prompt = DEFAULT_IMAGE_TOKEN + "\n" + question2
            print('prompt', prompt)
            
            conv.append_message(conv.roles[0], prompt)
            conv.append_message(conv.roles[1], None)
            prompt = conv.get_prompt()

            image_clip = data_item['image_trivial']
            image_clip = image_clip.unsqueeze(0).to(device)
            assert args.precision == "bf16"
            image_clip = image_clip.bfloat16()
            
            image = image_clip

            input_ids = tokenizer_image_token(prompt, tokenizer, return_tensors="pt")
            input_ids = input_ids.unsqueeze(0).to(device)

            output_ids, float_preds, seg_token_mask = model.evaluate(
                image_clip,
                image,
                input_ids,
                max_new_tokens=2048,
                tokenizer=tokenizer,
            )

            output_ids = output_ids[0, 1:]
            text_output = tokenizer.decode(output_ids, skip_special_tokens=False).strip().replace("</s>", "")
            
            print(f"text_output:    ???{text_output}???")
            text_output = text_output.replace('[STARTS]', '').replace('[SEG]', '').replace('[ENDS]', '')
            answers.append(text_output)

            garment_id = data_item['id'].split('/')[-1]
            garment_id = garment_id.split('.')[0]
            json_output = repair_json(text_output, return_objects=True)

            saved_dir = os.path.join(parent_folder, 'vis_new', f'valid_garment_{garment_id}')
            if not os.path.exists(saved_dir):
                os.makedirs(saved_dir)
            
            with open(os.path.join(saved_dir, 'output.txt'), 'w') as f:
                f.write(str(data_item['prompt']))
                f.write('\n\n')
                f.write(gpt_4o_description)
                f.write('\n\n')
                f.write(text_output)
                f.write('\n\n')
                f.write(str(json_output))
            
            with open(os.path.join(saved_dir, 'output.yaml'), 'w') as f:
                yaml.dump(json_output, f)

            output_dir = saved_dir
            all_output_dir.append(output_dir)
            # try:
            if True:
                float_preds = float_preds.reshape(-1)
                # assert len(float_preds) == len(all_float_paths)
                float_preds = float_preds[-len(all_float_paths):]
                float_dict = {
                    k: v for k, v in zip(all_float_paths, float_preds)
                }
                wholebody_config = json_output
                try_generate_garments(None, wholebody_config, 'wholebody', output_dir, invnorm_float=True, float_dict=float_dict)

                all_json_spec_files.append(
                    os.path.join(output_dir, 'valid_garment_wholebody', f'valid_garment_wholebody_specification.json')
                )
            # except:
            #     print('error')
            #     continue
        
    saved_json_Path = os.path.join(parent_folder, 'vis_new', 'all_json_spec_files.json')
    with open(saved_json_Path, 'w') as f:
        json.dump(all_json_spec_files, f)


        
if __name__ == "__main__":
    main(sys.argv[1:])         

