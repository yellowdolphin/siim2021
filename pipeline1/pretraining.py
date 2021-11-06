import gc
import importlib
import os
import sys
import random
import cv2
import neptune
import numpy as np
import pandas as pd
import torch
import albumentations 
from glob import glob
from utils.config import cfg
from utils.map_func import val_map
from utils.evaluate import val
from dataset.dataset import C14Dataset
from torch.utils.data import  DataLoader
from torch.cuda.amp import GradScaler, autocast
import torch.nn.functional as F
from sklearn.metrics import f1_score
from sklearn.metrics import accuracy_score, roc_auc_score, average_precision_score
from transformers import get_cosine_schedule_with_warmup, get_linear_schedule_with_warmup
from sklearn.model_selection import KFold
from utils.parallel import DataParallelModel, DataParallelCriterion
import time 
import neptune
from shutil import copyfile
from tqdm import tqdm
from warnings import filterwarnings
filterwarnings("ignore")

sys.path.append("models")

SIIMModel = importlib.import_module(cfg["model"]).SIIMModel

def set_seed(seed=1234):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.benchmark = True


def sigmoid_rampup(current, rampup_length=15):
    """Exponential rampup from https://arxiv.org/abs/1610.02242"""
    if rampup_length == 0:
        return 1.0
    else:
        current = np.clip(current, 0.0, rampup_length)
        phase = 1.0 - current / rampup_length
        return 0.9*float(np.exp(-5.0 * phase * phase))

def get_model(cfg):
    model = SIIMModel(model_name=cfg.model_architecture, pretrained=True,out_dim=cfg.output_size, pool=cfg.pool, dropout = cfg.dropout)
    return model

def logfile(message):
    print(message)
    with open(log_path, 'a+') as logger:
        logger.write(f'{message}\n')

def get_optimizer(cfg, model):
    if isinstance(cfg["lr"], list):
        params = [
            {
                "params": [
                    param for name, param in model.named_parameters() if "head" not in name
                ],
                "lr": cfg["train_params"]["lr"][0],
            },
            {
                "params": [param for name, param in model.named_parameters() if "head" in name],
                "lr": cfg["train_params"]["lr"][1],
            },
        ]
    else:
        params = [
            {
                "params": [param for name, param in model.named_parameters()],
                "lr": cfg["lr"],
            }
        ]

    if cfg["optimizer"] == "Adam":
        optimizer = torch.optim.Adam(params, lr=params[0]["lr"])
    elif cfg["optimizer"] == "SGD":
        optimizer = torch.optim.SGD(params, lr=params[0]["lr"], momentum=0.9, nesterov=True,)

    return optimizer

def get_scheduler(cfg, optimizer, total_steps):
    # print(total_steps)
    if cfg["scheduler"] == "steplr":
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=40000, gamma=0.8)
    elif cfg["scheduler"] == "cosine":
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=0,
            num_training_steps=(total_steps // cfg["batch_size"]),
        )
    elif cfg["scheduler"] == "linear":
        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_warmup_steps=0,
            num_training_steps=(total_steps // cfg["batch_size"]),
        )

        # print("num_steps", (total_steps // cfg["batch_size"]))
    elif cfg["scheduler"] == "onecycle":
        scheduler = optim.lr_scheduler.OneCycleLR(
            optimizer,
            max_lr=cfg["train_params"]["lr"],
            total_steps=total_steps // cfg["batch_size"] // 50,  # only go for 50 % of train
            pct_start=0.01,
            anneal_strategy="cos",
            cycle_momentum=True,
            base_momentum=0.85,
            max_momentum=0.95,
            div_factor=1e2,
            final_div_factor=1e5,
        )
    else:
        scheduler = None

    return scheduler

def get_dataloader(cfg, fold_id):
    if cfg.augmentation:
        print("[ √ ] Using augmentation file", f'configs/aug/{cfg.augmentation}')
        if 'yaml' in cfg.augmentation:
            transforms_train = albumentations.load(f'configs/aug/{cfg.augmentation}', data_format='yaml')
        else:
            transforms_train = albumentations.load(f'configs/aug/{cfg.augmentation}', data_format='json')
    else:
        transforms_train = albumentations.Compose([
            albumentations.Resize(cfg.input_size, cfg.input_size),
            albumentations.HorizontalFlip(p=0.5),
            albumentations.ShiftScaleRotate(p=0.7, shift_limit=0.0625, scale_limit=0.2, rotate_limit=20),
            albumentations.Cutout(p=0.5, max_h_size=16, max_w_size=16, fill_value=(0., 0., 0.), num_holes=16),
            # albumentations.Normalize(),
        ])

    transforms_valid = albumentations.Compose([
        albumentations.Resize(cfg.input_size, cfg.input_size),
        # albumentations.Normalize()
    ])

    if cfg.stage > 0:
        df = pd.read_csv(f'{cfg.out_dir}/oofs{cfg.stage-1}.csv')
    else:
        df = pd.read_csv(cfg.train_csv_path)

    kf = KFold(n_splits=5, shuffle=True, random_state = 42)
    df['fold'] = -1
    for fold_id, (train_index, test_index) in enumerate(kf.split(df)):
        df.iloc[test_index, -1] = fold_id

    if 'lateral' in cfg.out_dir or 'inverted' in cfg.out_dir:
        train_df = df[df['fold'] == 1]
    else:
        train_df = df[df['fold'] != fold_id]
    # 
    val_df = df[df['fold'] == fold_id]

    if cfg.mode in ['pseudo']:
        val_df = df

    if cfg.debug:
        train_df = train_df.head(100)
        val_df = val_df.head(100)

    train_dataset = C14Dataset(train_df, tfms=transforms_train, cfg=cfg)
    train_loader = DataLoader(train_dataset, batch_size=cfg.batch_size, shuffle=True,  num_workers=8, pin_memory=True)
    total_steps = len(train_dataset)

    val_dataset = C14Dataset(val_df, tfms=transforms_valid, cfg=cfg, mode='val')
    val_loader = DataLoader(val_dataset, batch_size=cfg.batch_size, shuffle=False,  num_workers=8, pin_memory=True)

    return train_loader, val_loader, total_steps, val_df

def train_func(model, train_loader, scheduler, device, epoch):
    model.train()
    start_time = time.time()
    losses = []
    bar = tqdm(train_loader)
    for batch_idx, batch_data in enumerate(bar):
        if cfg.use_seg:
            images, targets, oof_targets, targets1, hms = batch_data
        else:
            images, targets = batch_data

        if cfg["mixed_precision"]:
            with autocast():
                if cfg.use_seg:
                    prediction, seg_out = model(images.to(device))
                else:
                    prediction = model(images.to(device))
        else:
            if cfg.use_seg:
                prediction, seg_out = model(images.to(device))
            else:
                prediction = model(images.to(device))

        # print(targets.shape, prediction.shape)
        if cfg.loss == 'ce':
            loss = ce_criterion(prediction, targets1.to(device))
        elif cfg.loss == 'bce':
            loss = criterion(prediction, targets.to(device))
        else:
            loss = 0.2*ce_criterion(prediction1, targets1.to(device)) + 0.5*criterion(prediction, targets.to(device))

        if cfg.stage>0:
            # loss += 0.5*criterion(prediction, oof_targets.to(device))
            loss = (loss + cfg.stage*criterion(prediction, oof_targets.float().to(device)))/(1+cfg.stage)
        

        if cfg.use_seg:
            hm_loss = seg_criterion(seg_out[:,0,:,:], hms[:,:,:,0].to(device))
            
            ratio = sigmoid_rampup(epoch, cfg.epochs)
            ratio = 10*(1-ratio)

            loss += ratio*hm_loss
        
        if cfg["mixed_precision"]:
            scaler.scale(loss).backward()
            if (batch_idx+1) % cfg.accumulation_steps ==0 :
                scaler.unscale_(optimizer)
                scaler.step(optimizer)
                scaler.update()
        else:
            loss.backward()
            if (batch_idx+1) % cfg.accumulation_steps ==0 :
                optimizer.step()
                optimizer.zero_grad()

        scheduler.step()

        losses.append(loss.item())
        smooth_loss = np.mean(losses[-30:])

        if cfg.use_seg:
            bar.set_description(f'loss: {loss.item():.5f}, hm_loss: {hm_loss.item():.5f}, smth: {smooth_loss:.5f}, LR {scheduler.get_lr()[0]:.6f}')
        else:
            bar.set_description(f'loss: {loss.item():.5f}, smth: {smooth_loss:.5f}, LR {scheduler.get_lr()[0]:.6f}')

        if cfg.neptune_project:
            neptune.log_metric('train loss', smooth_loss)
            neptune.log_metric('LR', scheduler.get_lr()[0])

        if batch_idx>10 and cfg.debug:
            break

    loss_train = np.mean(losses)
    return loss_train

def valid_func(model, valid_loader):
    model.eval()
    bar = tqdm(valid_loader)

    pred_results  = []
    origin_labels = []

    pred_probs = []

    losses = []
    with torch.no_grad():
        for batch_idx, batch_data in enumerate(bar):
            if cfg.use_seg:
                images, targets, image_ids, targets1, hms = batch_data
            else:
                images, targets = batch_data

            images, targets = images.to(device), targets.to(device)
            origin_labels.append(targets.detach().cpu().numpy())


            if cfg.use_seg:
                logits, seg_out = model(images)
            else:
                logits = model(images)

            if cfg.loss == 'ce':
                loss = ce_criterion(logits, targets1.to(device))
            elif cfg.loss == 'bce':
                loss = criterion(logits, targets)
            else:
                loss = 0.2*ce_criterion(logits1, targets1.to(device)) + 0.5*criterion(logits, targets)

            if cfg.model in ['model_2']: #use bceloss
                prediction = logits
            else:
                if cfg.loss == 'bce':
                    prediction = F.sigmoid(logits)
                elif cfg.loss == 'ce':
                    prediction = F.softmax(logits)
                else:
                    prediction = 0.5*F.sigmoid(logits) + 0.5*F.softmax(logits1)

            proba = prediction.detach().cpu().numpy()

            pred_probs.append(proba)

            events = proba >= 0.5
            pred_labels = events.astype(np.int)
            
            pred_results.append(pred_labels)
            

            losses.append(loss.item())
            smooth_loss = np.mean(losses[-30:])

            bar.set_description(f'loss: {loss.item():.5f}, smth: {smooth_loss:.5f}')

            if batch_idx>30 and cfg.debug:
                break
            
    pred_results = np.concatenate(pred_results)    
    origin_labels = np.concatenate(origin_labels)
    pred_probs = np.concatenate(pred_probs)

    aucs = []
    acc = []
    for i in range(origin_labels.shape[1]):
        aucs.append(roc_auc_score(origin_labels[:, i], pred_probs[:, i]))
        acc.append(average_precision_score(origin_labels[:, i], pred_probs[:, i]))

    # print(np.round(aucs, 4))
    print("AUC list: ", np.round(aucs, 4), 'mean: ', np.mean(aucs))
    print("ACC list: ", np.round(acc, 4), 'mean: ', np.mean(acc))

    micro_score = f1_score(origin_labels, pred_results, average='micro')
    macro_score = f1_score(origin_labels, pred_results, average='macro')

    loss_valid = np.mean(losses)

    auc = np.mean(aucs)

    # map, ap_list = val_map(origin_labels, pred_probs)
    map = np.mean(acc)

    if cfg.neptune_project and cfg.mode == 'train':
        neptune.log_metric('VAL loss', loss_valid)
        neptune.log_metric('VAL micro f1 score', micro_score)
        neptune.log_metric('VAL macro f1 score', macro_score)
        neptune.log_metric('VAL auc', auc)
        neptune.log_metric('VAL map', map)
        for cc, ap in enumerate(ap_list):
            neptune.log_metric(f'VAL map cls {cc}', ap)

    return loss_valid, micro_score, macro_score, auc, map, pred_probs

def test_func(model, valid_loader):
    model.eval()
    bar = tqdm(valid_loader)

    pred_probs = []

    with torch.no_grad():
        for batch_idx, batch_data in enumerate(bar):
            images = batch_data

            # for img in images:
            #     print(img.shape)
            # print('===')
            images = images.to(device)

            if cfg.use_seg:
                logits, seg_out = model(images)
            else:
                logits = model(images)

            if cfg.model in ['model_2']: #use bceloss
                prediction = logits
            else:
                if cfg.loss == 'bce':
                    prediction = F.sigmoid(logits)
                elif cfg.loss == 'ce':
                    prediction = F.softmax(logits)
                else:
                    prediction = 0.5*F.sigmoid(logits) + 0.5*F.softmax(logits1)

            proba = prediction.detach().cpu().numpy()

            pred_probs.append(proba)

            if batch_idx>30 and cfg.debug:
                break
            
    pred_probs = np.concatenate(pred_probs)

    return pred_probs

if __name__ == "__main__":
    set_seed(cfg["seed"])

    if cfg.model in ['model_4']:
        print("[ √ ] Using segmentation")
        cfg.use_seg = True

    device = torch.device('cuda', 0) if torch.cuda.is_available() else torch.device('cpu')

    copyfile(os.path.basename(__file__), os.path.join(cfg.out_dir, os.path.basename(__file__)))

    oofs = []
    for cc, fold_id in enumerate(cfg.folds):
        log_path = f'{cfg.out_dir}/log_f{fold_id}_st{cfg.stage}.txt'
        train_loader, valid_loader, total_steps, val_df = get_dataloader(cfg, fold_id)
        total_steps = total_steps*cfg.epochs
        print(f'======== FOLD {fold_id} ========')
        if cfg.dp:
            model = torch.nn.DataParallel(model)

        if cfg.mode == 'train':
            logfile(f'======= STAGE {cfg.stage} =========')
            model = get_model(cfg).to(device)
            optimizer = get_optimizer(cfg, model)
            scheduler = get_scheduler(cfg, optimizer, total_steps)

            if cfg["mixed_precision"]:
                scaler = GradScaler()

            if cfg.model in ['model_2']:
                criterion = torch.nn.BCELoss()
            else:
                criterion = torch.nn.BCEWithLogitsLoss()
                
            ce_criterion = torch.nn.CrossEntropyLoss()

            if cfg.use_seg:
                seg_criterion = torch.nn.MSELoss()

            if cfg.resume_training:
                chpt_path = f'{cfg.out_dir}/last_checkpoint_fold{fold_id}_st{cfg.stage}.pth'
                checkpoint = torch.load(chpt_path, map_location="cpu")
                model.load_state_dict(checkpoint["model"])
                optimizer.load_state_dict(checkpoint["optimizer"])
                if cfg["mixed_precision"]:
                    scaler.load_state_dict(checkpoint["scaler"])
                if scheduler is not None:
                    scheduler.load_state_dict(checkpoint["scheduler"])

                del checkpoint
                gc.collect()

            if cfg.neptune_project:
                with open('resources/neptune_api.txt') as f:
                    token = f.read().strip()
                neptune.init(cfg.neptune_project, api_token = token)
                neptune.create_experiment(name=cfg.name, params=cfg)
                neptune.append_tag(cfg.model_architecture)
                neptune.append_tag(f'fold {fold_id}')
                neptune.append_tag(f'use_seg {cfg.use_seg}')
                neptune.append_tag(f'{cfg.model}')

        
            loss_min = 1e6
            map_score_max = 0
            for epoch in range(1, cfg.epochs+1):
                logfile(f'====epoch {epoch} ====')
                loss_train = train_func(model, train_loader, scheduler, device, epoch)
                loss_valid, micro_score, macro_score, auc, map, pred_probs = valid_func(model, valid_loader)

                if map > map_score_max:
                    logfile(f'map_score_max ({map_score_max:.6f} --> {map:.6f}). Saving model ...')
                    if cfg.use_seg:
                        torch.save(model.model.state_dict(), f'{cfg.out_dir}/best_map_fold{fold_id}_st{cfg.stage}.pth')
                    else:
                        torch.save(model.state_dict(), f'{cfg.out_dir}/best_map_fold{fold_id}_st{cfg.stage}.pth')
                    map_score_max = map

                if loss_valid < loss_min:
                    logfile(f'loss_min ({loss_min:.6f} --> {loss_valid:.6f}). Saving model ...')
                    loss_min = loss_valid
                    if cfg.use_seg:
                        torch.save(model.model.state_dict(), f'{cfg.out_dir}/best_loss_fold{fold_id}_st{cfg.stage}.pth')
                    else:
                        torch.save(model.state_dict(), f'{cfg.out_dir}/best_loss_fold{fold_id}_st{cfg.stage}.pth')

                if epoch == cfg.epochs:
                    if cfg.use_seg:
                        torch.save(model.model.state_dict(), f'{cfg.out_dir}/last_checkpoint_fold{fold_id}_st{cfg.stage}.pth')
                    else:
                        torch.save(model.state_dict(), f'{cfg.out_dir}/last_checkpoint_fold{fold_id}_st{cfg.stage}.pth')
                else:
                    checkpoint = {
                        "model": model.state_dict(),
                        "optimizer": optimizer.state_dict(),
                    }
                    if scheduler is not None:
                        checkpoint["scheduler"] = scheduler.state_dict()

                    if cfg["mixed_precision"]:
                        checkpoint["scaler"] = scaler.state_dict()

                    torch.save(checkpoint, f'{cfg.out_dir}/last_checkpoint_fold{fold_id}_st{cfg.stage}.pth')

                logfile(f'[EPOCH {epoch}] micro f1 score: {micro_score}, macro_score f1 score: {macro_score}, val loss: {loss_valid}, AUC: {auc}, MAP: {map}')

            if cfg.neptune_project and cfg.mode == 'train':
                neptune.stop()

            del model, scheduler, optimizer
            gc.collect()

        if cfg.mode == 'test':
            transforms_valid = albumentations.Compose([
                albumentations.Resize(cfg.input_size, cfg.input_size),
            ])
            val_df = pd.read_csv('data/edata_all1.csv')
            val_dataset = C14Dataset(val_df, tfms=transforms_valid, cfg=cfg, mode='test')
            val_loader = DataLoader(val_dataset, batch_size=cfg.batch_size, shuffle=False,  num_workers=8, pin_memory=True)

            model = get_model(cfg).to(device)
            # chpt_path = f'{cfg.out_dir}/last_checkpoint_fold{fold_id}_st{cfg.stage}.pth'
            # chpt_path = f'{cfg.out_dir}/best_map_fold{fold_id}_st{cfg.stage}.pth'
            chpt_path = f'{cfg.out_dir}/best_loss_fold{fold_id}_st{cfg.stage}.pth'
            checkpoint = torch.load(chpt_path, map_location="cpu")
            model.load_state_dict(checkpoint)
            del checkpoint
            gc.collect()

            pred_probs = test_func(model, val_loader)

            val_df[f'score'] = pred_probs

            val_df.to_csv(f'{cfg.out_dir}/{cfg.name}_{cfg.mode}_st{cfg.stage}.csv', index=False)

            del model 
            gc.collect()