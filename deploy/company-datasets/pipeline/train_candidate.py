"""Bounded QLoRA candidate training, with held-out loss; no model deployment."""
import argparse,json,hashlib
from pathlib import Path
import torch
from transformers import AutoTokenizer,AutoModelForCausalLM,BitsAndBytesConfig,Trainer,TrainingArguments
from peft import LoraConfig,get_peft_model,prepare_model_for_kbit_training

def load(path):
    rows=[json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    if not rows:raise ValueError('Empty dataset')
    for r in rows:
        if r.get('lane')!='cory_public' or r.get('channel')!='phone':raise ValueError('Wrong training lane')
        if r['messages'][-1]['role']!='assistant':raise ValueError('Missing assistant target')
    return rows

def tokenize(tokenizer,rows):
    result=[]
    for row in rows:
        messages=row['messages']
        full=tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=False,enable_thinking=False)
        prefix=tokenizer.apply_chat_template(messages[:-1],tokenize=True,add_generation_prompt=True,enable_thinking=False)
        # Never invent a target when truncation removes the assistant reply.
        if full[:len(prefix)]!=prefix or len(prefix)>=2048:continue
        ids=full[:2048]
        if len(ids)<=len(prefix):continue
        result.append({'input_ids':ids,'attention_mask':[1]*len(ids),'labels':[-100]*len(prefix)+ids[len(prefix):]})
    if not result:raise ValueError('No valid supervised replies after tokenization')
    return result

def main():
    p=argparse.ArgumentParser()
    for n in ['model','data','eval-data','output']:p.add_argument('--'+n,required=True)
    a=p.parse_args();out=Path(a.output)
    train=load(a.data);evaluation=load(a.eval_data)
    if {x['source_id'] for x in train}&{x['source_id'] for x in evaluation}:raise ValueError('Train/eval leakage')
    tokenizer=AutoTokenizer.from_pretrained(a.model,trust_remote_code=False)
    tokenizer.pad_token=tokenizer.eos_token
    train_tokens=tokenize(tokenizer,train);eval_tokens=tokenize(tokenizer,evaluation)
    model=AutoModelForCausalLM.from_pretrained(a.model,trust_remote_code=False,device_map={'':0},
        quantization_config=BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_quant_type='nf4',bnb_4bit_use_double_quant=True,bnb_4bit_compute_dtype=torch.float16),
        torch_dtype=torch.float16)
    model.config.use_cache=False
    model=prepare_model_for_kbit_training(model)
    model=get_peft_model(model,LoraConfig(r=16,lora_alpha=32,lora_dropout=.05,bias='none',task_type='CAUSAL_LM',
        target_modules=['q_proj','k_proj','v_proj','o_proj','gate_proj','up_proj','down_proj']))
    def collate(batch):
        length=max(len(r['input_ids']) for r in batch)
        return {key:torch.tensor([r[key]+[pad]*(length-len(r[key])) for r in batch])
                for key,pad in [('input_ids',tokenizer.pad_token_id),('attention_mask',0),('labels',-100)]}
    args=TrainingArguments(output_dir=str(out/'checkpoints'),per_device_train_batch_size=1,per_device_eval_batch_size=1,
        gradient_accumulation_steps=8,num_train_epochs=1,learning_rate=5e-5,fp16=True,gradient_checkpointing=True,
        logging_steps=10,save_strategy='no',report_to=[],optim='paged_adamw_8bit',seed=42)
    trainer=Trainer(model=model,args=args,train_dataset=train_tokens,eval_dataset=eval_tokens,data_collator=collate)
    baseline=trainer.evaluate();train_metrics=trainer.train().metrics;candidate=trainer.evaluate()
    model.save_pretrained(out/'adapter');tokenizer.save_pretrained(out/'adapter')
    (out/'evaluation.json').write_text(json.dumps({'baseline':baseline,'candidate':candidate,'train':train_metrics,
        'train_records':len(train_tokens),'eval_records':len(eval_tokens),'base_model':a.model,
        'train_sha256':hashlib.sha256(Path(a.data).read_bytes()).hexdigest(),
        'promotion':'not_deployed_requires_call_tool_safety_evaluation'},indent=2))

if __name__=='__main__':main()
