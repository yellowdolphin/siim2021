import os

outputdir = "outputs/" + os.path.basename(__file__).split(".")[0]

cfg = {
    "debug": 0,
    "name": os.path.basename(__file__).split(".")[0],
    "model_architecture": "eca_nfnet_l1",  # resnet200d, tf_efficientnetv2_m, eca_nfnet_l1, seresnext26tn_32x4d tf_efficientnet_b3_ns nf_regnet_b1 dm_nfnet_f1 densenet121 dm_nfnet_f4
    #"image_dir": "data/png512",
    "image_dir": "/kaggle/input/siim-covid19-resized-to-512px-png",
    "train_csv_path": "data/train_split_seed42.csv",
    "input_size": 384,
    "output_size": 4,
    "use_seg":False,
    "out_dir": f"{outputdir}",
    "folds": [0, 1, 2, 3, 4],
    "augmentation": "faster.yaml",
    #"weight_file": 'outputs/n_cf2_pretraining/eca_nfnet_l1b/best_map_fold0_st0.pth',  # "/model_state_45000.pth",
    "weight_file": '/kaggle/input/siimnihpretrained/n_cf2_pretraining/eca_nfnet_l1b/best_map_fold0_st0.pth',
    # "weight_file":None,
    "resume_training": False,
    "dropout": 0.6,
    "pool": "gem",
    "batch_size": 32,
    "num_workers": 4,
    "optimizer": "AdamW",  # Adam, AdamW, SGD
    "lr": 5e-5,
    "mixed_precision": 0, 
    "distributed":0,
    "find_unused_parameters":0,
    "dp":0,
    "accumulation_steps": 1,
    "seed": 42,
    "neptune_project": None,  
    # "neptune_project": "nvnn/siim2021",
    "scheduler": "cosine", #linear  cosine
    "model": "model_1",
    "epochs": 10,
    "mode": "train",
    "loss": 'bce',
    "muliscale": 0,
}

# swin_large_patch4_window12_384 vit_large_patch16_384
