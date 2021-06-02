import os

outputdir = "outputs/" + os.path.basename(__file__).split(".")[0]

cfg = {
    "debug": False,
    "name": os.path.basename(__file__).split(".")[0],
    "model_architecture": "eca_nfnet_l1",  # resnet200d, tf_efficientnetv2_m, eca_nfnet_l1, seresnext26tn_32x4d tf_efficientnet_b3_ns
    "image_dir": "data/png512",
    "train_csv_path": "data/train_split_seed42.csv",
    "input_size": 384,
    "output_size": 4,
    "use_seg":False,
    "out_dir": f"{outputdir}",
    "folds": [0],
    "augmentation": "s_0220/0220_hf_cut_sm2_0.75_384.yaml",
    "weight_file": None,  # "/model_state_45000.pth",
    "resume_training": False,
    "dropout": 0.5,
    "pool": "gem",
    "batch_size": 8,
    "num_workers": 8,
    "optimizer": "Adam",  # Adam, SGD
    "lr": 1e-4,
    "mixed_precision": False, 
    "seed": 42,
    "neptune_project": None,  
    # "neptune_project": "nvnn/siim2021",
    "scheduler": "cosine",
    "model": "model_4",
    "epochs": 15,
    "type": "train"
}

