# SIIM COVID 2021
## Data
* Download dataset to data/
* run `python create_folds.py` to split train data into folds
* pretrained checkpoint on NIH dataset: https://www.kaggle.com/ammarali32/startingpointschestx

## Train
* Create config file for each experiments, for example configs/n_cf1.py
* Train: `python main.py -C n_cf1`

