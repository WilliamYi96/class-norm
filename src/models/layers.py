import functools
from typing import Tuple, Any, Iterable, Callable

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from torchvision.models.resnet import resnet18, resnet34, resnet50
from firelab.config import Config

input_type_TO_CLS = {18: resnet18, 34: resnet34, 50: resnet50}


class ConditionalBatchNorm2d(nn.Module):
    """
    Conditional Batch Normalization by @Kaixhin
    https://github.com/pytorch/pytorch/issues/8985
    """
    def __init__(self, num_features, num_classes):
        super().__init__()
        self.num_features = num_features
        self.bn = nn.BatchNorm2d(num_features, affine=False)
        self.embed = nn.Embedding(num_classes, num_features * 2)
        self.embed.weight.data[:, :num_features].normal_(1, 0.02)  # Initialise scale at N(1, 0.02)
        self.embed.weight.data[:, num_features:].zero_()  # Initialise bias at 0

    def forward(self, x, y):
        out = self.bn(x)
        gamma, beta = self.embed(y).chunk(2, 1)
        out = gamma.view(-1, self.num_features, 1, 1) * out + beta.view(-1, self.num_features, 1, 1)

        return out


class Reshape(nn.Module):
    def __init__(self, target_shape: Tuple[int]):
        super(Reshape, self).__init__()

        self.target_shape = target_shape

    def forward(self, x):
        return x.view(*self.target_shape)


class Flatten(nn.Module):
    def __init__(self):
        super(Flatten, self).__init__()

    def forward(self, x):
        return x.view(x.size(0), -1)


class ResNetLastBlock(nn.Module):
    def __init__(self, input_type: int, pretrained: bool, should_pool: bool=True):
        super(ResNetLastBlock, self).__init__()

        self.resnet = input_type_TO_CLS[input_type](pretrained=pretrained)
        self.should_pool = should_pool

        del self.resnet.conv1
        del self.resnet.bn1
        del self.resnet.relu
        del self.resnet.maxpool
        del self.resnet.layer1
        del self.resnet.layer2
        del self.resnet.layer3
        del self.resnet.fc

    def forward(self, x: Tensor) -> Tensor:
        x = self.resnet.layer4(x)

        if self.should_pool:
            x = self.resnet.avgpool(x)
            x = torch.flatten(x, 1)

        return x


class ResNetConvEmbedder(nn.Module):
    def __init__(self, input_type: int, pretrained: bool):
        super(ResNetConvEmbedder, self).__init__()

        self.resnet = input_type_TO_CLS[input_type](pretrained=pretrained)

        del self.resnet.layer4
        del self.resnet.avgpool
        del self.resnet.fc

    def forward(self, x: Tensor) -> Tensor:
        x = self.resnet.conv1(x)
        x = self.resnet.bn1(x)
        x = self.resnet.relu(x)
        x = self.resnet.maxpool(x)

        x = self.resnet.layer1(x)
        x = self.resnet.layer2(x)
        x = self.resnet.layer3(x)

        return x


class Identity(nn.Module):
    def __init__(self):
        super(Identity, self).__init__()

        self.dummy_param = nn.Parameter(torch.zeros(1))

    def forward(self, x: Any) -> Any:
        return x


class ConvBNReLU(nn.Module):
    def __init__(self, num_in_c: int, num_out_c: int, kernel_size: int, maxpool: bool=False, *conv_args, **conv_kwargs):
        super(ConvBNReLU, self).__init__()

        self.block = nn.Sequential(
            nn.Conv2d(num_in_c, num_out_c, kernel_size, *conv_args, **conv_kwargs),
            (nn.MaxPool2d(2, 2) if maxpool else Identity()),
            nn.BatchNorm2d(num_out_c),
            nn.ReLU()
        )

    def forward(self, x: Tensor) -> Tensor:
        return self.block(x)


class ConvTransposeBNReLU(nn.Module):
    def __init__(self, num_in_c: int, num_out_c: int, kernel_size: int, *conv_args, **conv_kwargs):
        super(ConvTransposeBNReLU, self).__init__()

        self.block = nn.Sequential(
            nn.ConvTranspose2d(num_in_c, num_out_c, kernel_size, *conv_args, **conv_kwargs),
            nn.BatchNorm2d(num_out_c),
            nn.ReLU(),
        )

    def forward(self, x):
        return self.block(x)


class RepeatToSize(nn.Module):
    def __init__(self, target_size: int):
        super(RepeatToSize, self).__init__()

        self.target_size = target_size

    def forward(self, x: Tensor) -> Tensor:
        assert x.ndim == 2

        return x.view(x.size(0), x.size(1), 1, 1).repeat(1, 1, self.target_size, self.target_size)


class GaussianDropout(nn.Module):
    def __init__(self, sigma: float):
        super(GaussianDropout, self).__init__()
        self.sigma = sigma

    def forward(self, x):
        if self.training or self.sigma == 0:
            return x
        else:
            return x + self.sigma * torch.randn_like(x)


class FeatEmbedder(nn.Module):
    def __init__(self, config: Config, use_attrs: bool=False, attrs: np.ndarray=None):
        super(FeatEmbedder, self).__init__()

        self.config = config
        self.use_attrs = use_attrs

        if self.use_attrs:
            self.register_buffer('attrs', torch.tensor(attrs))
            self.model = nn.Linear(self.attrs.size[1], self.config.emb_dim)
        else:
            self.model = nn.Embedding(self.config.num_classes, self.config.emb_dim)

    def forward(self, y: Tensor) -> Tensor:
        # TODO: let's use both attrs and class labels!
        inputs = self.attrs[y] if self.use_attrs else y

        return self.model(inputs)


class EqualLRLinear(nn.Module):
    def __init__(self, n_in: int, n_out: int, init_strategy: str):
        super().__init__()

        self.n_in = n_in
        self.n_out = n_out
        self.init_strategy = init_strategy

        self.weight = nn.Parameter(torch.randn(n_out, n_in))
        self.bias = nn.Parameter(torch.zeros(n_out))

    def get_std(self):
        if self.init_strategy == 'kaiming_fan_in':
            return np.sqrt(2 / self.n_in)
        elif self.init_strategy == 'kaiming_fan_out':
            return np.sqrt(2 / self.n_out)
        elif self.init_strategy == 'xavier':
            return np.sqrt(1 / (self.n_in + self.n_out))
        elif self.init_strategy == 'attrs':
            return np.sqrt(2 / (self.n_in * self.n_out * (1 - 1/np.pi)))
        else:
            raise ValueError(f'Unknown init strategy: {self.init_strategy}')

    def forward(self, x):
        W = self.weight * self.get_std()
        out = x @ W.t() + self.bias.unsqueeze(0)

        return out


class MILayer(nn.Module):
    """
    A Multiplicative Interaction layer: f(x, z) = z'Wx + z'U + Vx + b
    The first part (z'Wx) is MI, the second part (z'U + Vx + b) is equivalent to concatenation-based approach
    Reference: https://openreview.net/forum?id=rylnK6VtDH
    """
    def __init__(self, x_dim: int, z_dim: int, out_dim: int, combine_with_concat: bool=True):
        super().__init__()

        self.x_dim = x_dim
        self.z_dim = z_dim
        self.out_dim = out_dim
        self.combine_with_concat = combine_with_concat

        self.mi_layer = nn.Linear(z_dim, out_dim * x_dim) # Let's keep the bias since it's soo cheap

        if self.combine_with_concat:
            self.dense = nn.Linear(x_dim + z_dim, out_dim)

    def forward(self, x, z):
        assert x.ndim == z.ndim == 2
        assert x.size(0) == z.size(0)
        assert x.size(1) == self.x_dim
        assert z.size(1) == self.z_dim
        batch_size = x.size(0)

        contextualized_transform = self.mi_layer(z) # [batch_size, out_dim * x_dim]
        contextualized_transform = contextualized_transform.view(batch_size, self.x_dim, self.out_dim)
        result = x.view(batch_size, 1, self.x_dim) @ contextualized_transform
        result = result.squeeze(1)

        if self.combine_with_concat:
            result += self.dense(torch.cat([x, z], dim=1))

        assert result.shape == (x.size(0), self.out_dim), f"Wrong shape: {result.shape}"

        return result


class ConcatLayer(nn.Module):
    """
    Simple concatenation layer
    """
    def __init__(self, x_dim: int, z_dim: int, out_dim: int):
        super().__init__()
        self.model = nn.Linear(x_dim + z_dim, out_dim)

    def forward(self, x: Tensor, z: Tensor) -> Tensor:
        return self.model(torch.cat([x, z], dim=1))


class Fuser(nn.Module):
    def __init__(self, transform: nn.Module, activation: str='none'):
        super().__init__()

        self.transform = transform
        self.activation = create_activation(activation)

    def forward(self, x, z) -> Tensor:
        return self.activation(self.transform(x, z))


def create_fuser(fusing_type: str, input_size: int, context_size: int, output_size: int, activation: str='none') -> nn.Module:
    if fusing_type == 'pure_mult_int':
        transform = MILayer(input_size, context_size, output_size, False)
    if fusing_type == 'full_mult_int':
        transform = MILayer(input_size, context_size, output_size, True)
    elif fusing_type == 'concat':
        transform = ConcatLayer(input_size, context_size, output_size)
    else:
        raise NotImplementedError(f'Unknown fusing type: {fusing_type}')

    return Fuser(transform, activation)


def create_sequential_model(
        layers_sizes: Iterable[int],
        final_activation: bool=False,
        activation: str='relu',
        bias: bool=True) -> nn.Sequential:

    assert len(layers_sizes) > 0, "We need at least an input size"

    modules = []
    input_dim = layers_sizes[0]

    for output_dim in layers_sizes[1:]:
        modules.append(nn.Linear(input_dim, output_dim, bias=bias))
        modules.append(create_activation(activation))
        input_dim = output_dim

    if len(modules) > 0 and not final_activation:
        modules.pop(-1)

    return nn.Sequential(*modules)


def create_activation(activation: str) -> Callable:
    if activation == 'none':
        return nn.Identity()
    elif activation == 'relu':
        return nn.ReLU()
    elif activation == 'leaky_relu':
        return nn.LeakyReLU(0.2)
    elif activation == 'tanh':
        return nn.Tanh()
    else:
        raise NotImplementedError(f'Unknown activation type: {activation}')


def identity_init_(module: nn.Linear):
    n_in, n_out = module.weight.data.shape
    assert isinstance(module, nn.Linear), f"Not a linear module: {module}"
    assert n_in == n_out, f"Cannot use identity init for non-square transforms: {n_in, n_out}"
    module.weight.data.mul_(0.001)
    module.weight.data.add_(torch.eye(n_in))
