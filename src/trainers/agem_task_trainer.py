from typing import Tuple
import random

import torch
from torch import Tensor
import numpy as np

from src.trainers.task_trainer import TaskTrainer
from src.utils.training_utils import prune_logits
from src.utils.constants import NEG_INF
from src.utils.data_utils import construct_output_mask, flatten


class AgemTaskTrainer(TaskTrainer):
    def _after_init_hook(self):
        prev_trainer = self.get_previous_trainer()

        if self.task_idx == 0:
            self.episodic_memory = []
            self.episodic_memory_output_mask = []
        elif prev_trainer != None:
            prev_trainer = self.main_trainer.task_trainers[self.task_idx - 1]
            self.episodic_memory = prev_trainer.episodic_memory
            self.episodic_memory_output_mask = prev_trainer.episodic_memory_output_mask

    def is_trainable(self) -> bool:
        if not super().is_trainable:
            return False

        return self.task_idx == 0 or self.get_previous_trainer() != None

    def train_on_batch(self, batch: Tuple[Tensor, Tensor]):
        self.model.train()

        loss = self.compute_loss(self.model, batch)

        if self.task_idx - self.config.get('start_task', 0) > 0:
            assert len(self.episodic_memory) > 0

            ref_grad = self.compute_ref_grad()
            loss.backward()
            grad = torch.cat([p.grad.data.view(-1) for p in self.model.parameters() if p.requires_grad])
            grad = self.project_grad(grad, ref_grad)

            self.optim.zero_grad()
            self.set_grad(grad)
        else:
            self.optim.zero_grad()
            loss.backward()

        self.optim.step()

    def compute_ref_grad(self):
        num_samples_to_use = min(self.config.hp.mem_batch_size, len(self.episodic_memory))
        batch_idx = random.sample(np.arange(len(self.episodic_memory)).tolist(), num_samples_to_use)
        batch = [m for i, m in enumerate(self.episodic_memory) if i in batch_idx]
        output_mask = np.array([m for i, m in enumerate(self.episodic_memory_output_mask) if i in batch_idx])

        x = torch.from_numpy(np.array([x for x,_ in batch])).to(self.device_name)
        y = torch.tensor([y for _, y in batch]).to(self.device_name)
        logits = self.model(x)
        pruned_logits = logits.masked_fill(torch.tensor(~output_mask).to(self.device_name), NEG_INF)
        loss = self.criterion(pruned_logits, y)
        loss.backward()
        ref_grad = torch.cat([p.grad.data.view(-1) for p in self.model.parameters() if p.requires_grad])
        self.optim.zero_grad()

        return ref_grad

    def project_grad(self, grad, ref_grad):
        dot_product = torch.dot(grad, ref_grad)

        if dot_product > 0:
            return grad
        else:
            return grad - ref_grad * dot_product / ref_grad.pow(2).sum()

    def set_grad(self, grad: Tensor):
        """Takes gradient and sets it to parameters .grad"""
        assert grad.dim() == 1

        for param in self.model.parameters():
            if not param.requires_grad: continue
            param.grad.data = grad[:param.numel()].view(*param.shape).data
            grad = grad[param.numel():]

        assert len(grad) == 0, "Not all weights were used!"

    def update_episodic_memory(self):
        """
        Adds examples from own data to episodic memory

        :param:
            - task_idx — task index
            - num_samples_per_class — max number of samples of each class to add
        """
        num_samples_per_class = self.config.hp.num_mem_samples_per_class
        unique_labels = set([y for _, y in self.load_dataset(self.task_ds_train)])
        groups = [[(x,y) for (x,y) in self.load_dataset(self.task_ds_train) if y == label] for label in unique_labels] # Slow but concise
        num_samples_to_add = [min(len(g), num_samples_per_class) for g in groups]
        task_memory = [random.sample(g, n) for g, n in zip(groups, num_samples_to_add)]
        task_memory = flatten(task_memory)
        task_mask = construct_output_mask(self.main_trainer.class_splits[self.task_idx], self.config.lll_setup.num_classes)
        task_mask = task_mask.reshape(1, -1).repeat(len(task_memory), axis=0)

        assert len(task_memory) <= num_samples_per_class * len(groups)
        assert len(task_mask) <= num_samples_per_class * len(groups)

        self.episodic_memory.extend(task_memory)
        self.episodic_memory_output_mask.extend([m for m in task_mask])
