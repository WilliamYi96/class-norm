import os
from os import PathLike
from typing import List, Tuple, Any, Callable

import cv2
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import numpy as np
from tqdm import tqdm
from torchvision import transforms
from firelab.utils.training_utils import get_module_device
from torch.utils.data import Dataset
import torchvision.transforms.functional as TVF

from src.models.classifier import ResnetEmbedder
from src.models.layers import ResNetConvEmbedder
from src.utils.constants import IMAGENET_MEAN, IMAGENET_STD


def read_column(filename: PathLike, column_idx: int, sep: str=' ') -> List[str]:
    with open(filename) as f:
        column = [line.split(sep)[column_idx] for line in f.read().splitlines()]

    return column


def shuffle_dataset(imgs: List[Any], labels: List[int]) -> Tuple[List[Any], List[int]]:
    assert len(imgs) == len(labels)

    shuffling = np.random.permutation(len(imgs))
    imgs = [imgs[i] for i in shuffling]
    labels = [labels[i] for i in shuffling]

    return imgs, labels


def load_imgs_from_folder(image_folder: PathLike, img_paths: List[PathLike], *args, **kwargs) -> List[np.ndarray]:
    full_img_paths = [os.path.join(image_folder, p) for p in img_paths]

    return load_imgs(full_img_paths, *args, **kwargs)


def load_imgs(img_paths: List[PathLike], target_shape=None) -> List[np.ndarray]:
    return [load_img(p, target_shape) for p in tqdm(img_paths, desc='[Loading dataset]')]


def load_img(img_path: PathLike, target_shape: Tuple[int, int]=None, preprocess: bool=False):
    img = cv2.imread(img_path)

    # TODO: should we first resize and then preprocess or on the contrary?
    if preprocess:
        img = normalize_img(img)

    if target_shape != None:
        img = cv2.resize(img, target_shape)

    return img


def preprocess_imgs(imgs: List[np.ndarray]) -> List[np.ndarray]:
    return [normalize_img(img).transpose(2, 0, 1) for img in tqdm(imgs, desc='[Preprocessing]')]


def normalize_img(img: np.ndarray) -> np.ndarray:
    assert img.dtype == np.uint8, f"Wrong image type: {img.dtype}"
    assert img.shape[2] == 3, f"Wrong image shape: {img.shape}"

    img = img.astype(np.float32) / 255
    mean = IMAGENET_MEAN.reshape(1, 1, 3)
    std = IMAGENET_STD.reshape(1, 1, 3)
    img_normalized = (img - mean) / std

    return img_normalized.astype(np.float32)


def default_transform(img: np.ndarray, target_shape: Tuple[int]=None) -> np.ndarray:
    if target_shape is None:
        result = img
    else:
        result = cv2.resize(img, target_shape)

    result = normalize_img(result)
    result = result.transpose(2, 0, 1)

    return result


def create_default_transform(target_shape: Tuple[int]) -> np.ndarray:
    return lambda x: default_transform(x, target_shape)


def create_custom_dataset(paths_dataset, target_shape):
    def preprocessor(img):
        img = cv2.resize(img, target_shape)
        img = normalize_img(img).transpose(2, 0, 1)
        img = img.astype(np.float32)

        return img

    return CustomDataset(paths_dataset, preprocessor, load_from_disk=True)


def extract_resnet_features_for_dataset(
    dataset: List[Tuple[np.ndarray, int]],
    resnet_n_layers: int=18,
    feat_level: str='fc',
    device: str='cpu',
    *args, **kwargs) -> List[Tuple[np.ndarray, int]]:

    if feat_level == 'fc':
        embedder = ResnetEmbedder(resnet_n_layers=resnet_n_layers, pretrained=True).to(device)
    elif feat_level == 'conv':
        embedder = ResNetConvEmbedder(resnet_n_layers=resnet_n_layers, pretrained=True).to(device)
    else:
        raise NotImplementedError(f'Unknown feat level: {feat_level}')

    return extract_features_for_dataset(dataset, embedder, *args, **kwargs)


def extract_features_for_dataset(
    dataset: List[Tuple[np.ndarray, int]],
    embedder: nn.Module,
    device: str='cpu',
    batch_size: int=64) -> List[Tuple[np.ndarray, int]]:

    embedder = embedder.eval()
    embedder = embedder.to(device)

    imgs = [x for x, _ in dataset]
    features = extract_features(imgs, embedder, batch_size=batch_size)

    return list(zip(features, [y for _, y in dataset]))


def extract_features(imgs: List[np.ndarray], embedder: nn.Module, batch_size: int=64, verbose: bool=True) -> List[np.ndarray]:
    dataloader = DataLoader(imgs, batch_size=batch_size, num_workers=4)
    device = get_module_device(embedder)
    result = []

    with torch.no_grad():
        batches = tqdm(dataloader, desc='[Extracting features]') if verbose else dataloader
        for x in batches:
            feats = embedder(x.to(device)).cpu().numpy()
            result.extend(feats)

    return result


class CustomDataset(Dataset):
    def __init__(self, dataset: List, transform: Callable=None, load_from_disk: bool=False):
        # self.dataset = load_dataset(data_dir, split=('train' if train else 'test'))
        self.dataset = dataset
        self.transform = transform
        self.load_from_disk = load_from_disk

    def __getitem__(self, index):
        x, y = self.dataset[index]
        if self.load_from_disk: x = load_img(x)
        x = x.astype(np.uint8)

        if not self.transform is None:
            x = self.transform(x)

        return x, y

    def __len__(self) -> int:
        return len(self.dataset)


class CenterCropToMin(object):
    """
    CenterCrops an image to a min size
    """
    def __call__(self, img):
        assert TVF._is_pil_image(img)

        return TVF.center_crop(img, min(img.size))
