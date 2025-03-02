import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.nn.utils import clip_grad_norm_
from torch.optim import AdamW, Optimizer
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from .data import TTSCollator, TTSDataset
from .transformer import TTSTransformer, TTSTransformerArgs

logger = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    epochs: int = 100
    batch_size: int = 8
    learning_rate: float = 3e-4
    grad_accum_steps: int = 1
    grad_clip_norm: float = 1.0
    optimizer: str = "AdamW"
    lr_scheduler: str = "OneCycleLR"

    data_dir: Path = Path("data")
    checkpoint_dir: Path = Path("checkpoints")

    text_pad_id: int = 0
    audio_pad_id: int = 0

    num_workers: int = 4
    seed: int = 42
    backend: str = "nccl"

    checkpoint_freq: int = 1
    validation_freq: int = 1
    resume_checkpoint: Optional[Path] = None

    def post_init__(self):
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class TrainerState:
    epoch: int = 0
    global_step: int = 0
    best_loss: float = float("inf")


@dataclass
class CheckpointState:
    model_state: Dict[str, Any]
    optimizer_state: Dict[str, Any]
    scheduler_state: Optional[Dict[str, Any]]
    trainer_state: TrainerState
    config: TrainingConfig

    def save(self, path: Path):
        torch.save(
            {
                "model_state": self.model_state,
                "optimizer_state": self.optimizer_state,
                "scheduler_state": self.scheduler_state,
                "trainer_state": self.trainer_state,
                "config": self.config,
            },
            path,
        )

    @classmethod
    def load(cls, path: Path, device: torch.device) -> "CheckpointState":
        checkpoint = torch.load(path, map_location=device)
        return cls(
            model_state=checkpoint["model_state"],
            optimizer_state=checkpoint["optimizer_state"],
            scheduler_state=checkpoint.get("scheduler_state"),
            trainer_state=checkpoint["trainer_state"],
            config=checkpoint["config"],
        )


class DistributedTTSTrainer:
    def __init__(
        self, config: TrainingConfig, model: torch.nn.Module, device: torch.device
    ):
        self.config: TrainingConfig = config
        self.device = device
        self.is_main = dist.get_rank() == 0

        self.model = DDP(
            model.to(device), device_ids=[device.index], output_device=device.index
        )
        self.optimizer: Optimizer = self._create_optimizer()
        self.train_loader, self.val_loader = self._create_dataloaders()
        self.scheduler = self._create_scheduler()

        self.state = TrainerState()
        if self.config.resume_checkpoint:
            self.load_checkpoint()

    def _create_optimizer(self):
        if self.config.optimizer == "AdamW":
            return AdamW(self.model.parameters(), lr=self.config.learning_rate)
        else:
            return NotImplementedError(
                f"Optimizer: {self.config.optimizer} not implemented!"
            )

    def _create_scheduler(self):
        if self.config.lr_scheduler == "OneCycleLR":
            return torch.optim.lr_scheduler.OneCycleLR(
                self.optimizer,
                max_lr=self.config.learning_rate,
                total_steps=self.config.epochs
                * (len(self.train_loader) // self.config.grad_accum_steps),
            )
        else:
            return NotImplementedError(
                f"lr_scheduler:{self.config.lr_scheduler} not implemented!"
            )

    def _create_dataloaders(self):
        collator_fn = TTSCollator(self.config.text_pad_id, self.config.audio_pad_id)

        train_dataset = TTSDataset("train", self.config.data_dir)
        train_sampler = DistributedSampler(
            train_dataset,
            num_replicas=dist.get_world_size(),
            rank=dist.get_rank(),
            shuffle=True,
        )

        train_loader = DataLoader(
            train_dataset,
            batch_size=self.config.batch_size,
            sampler=train_sampler,
            collate_fn=collator_fn,
            num_workers=self.config.num_workers,
            pin_memory=True,
            persistent_workers=True,
        )

        val_dataset = TTSDataset("validation", self.config.data_dir)
        val_sampler = DistributedSampler(
            val_dataset,
            num_replicas=dist.get_world_size(),
            rank=dist.get_rank(),
            shuffle=False,
        )
        val_loader = DataLoader(
            val_dataset,
            batch_size=self.config.batch_size,
            sampler=val_sampler,
            collate_fn=collator_fn,
            num_workers=self.config.num_workers,
            pin_memory=True,
        )

        return train_loader, val_loader

    def save_checkpoint(self, best: bool = False):
        if not self.is_main:
            return

        checkpoint = CheckpointState(
            model_state=self.model.module.state_dict(),
            optimizer_state=self.optimizer.state_dict(),
            scheduler_state=self.scheduler.state_dict() if self.scheduler else None,
            trainer_state=self.state,
            config=self.config,
        )

        filename = f"checkpoint_{self.state.epoch:04d}.pt" if not best else "best.pt"
        path = self.config.checkpoint_dir / filename
        checkpoint.save(path)
        logger.info(f"Saved checkpoint to {path}")

    def load_checkpoint(self):
        checkpoint = CheckpointState.load(self.config.resume_checkpoint, self.device)

        self.model.module.load_state_dict(checkpoint.model_state)
        self.optimizer.load_state_dict(checkpoint.optimizer_state)

        if self.scheduler and checkpoint.scheduler_state:
            self.scheduler.load_state_dict(checkpoint.scheduler_state)

        self.state = checkpoint.trainer_state

        dist.broadcast_object_list([self.state.epoch, self.state.global_step], src=0)
        logger.info(
            f"Resumed training from {self.config.resume_checkpoint} at epoch {self.state.epoch}"
        )

    def train_epoch(self):
        self.model.train()
        self.train_loader.sampler.set_epoch(self.state.epoch)
        total_loss = 0.0
        accum_steps = 0

        for batch_idx, batch in enumerate(self.train_loader):
            batch = {k: v.to(self.device) for k, v in batch.items()}

            with torch.autocast(device_type="cuda", dtype=torch.float16):
                loss, _ = self.model(
                    text_tokens=batch["text_tokens"],
                    audio_tokens=batch["audio_tokens"][:, :, :-1],
                    target=batch["audio_tokens"][:, :, 1:],
                    mask=batch["attention_mask"],
                )

            scaled_loss = loss / self.config.grad_accum_steps
            scaled_loss.backward()
            total_loss += loss.item()
            accum_steps += 1

            if (batch_idx + 1) % self.config.grad_accum_steps == 0:
                total_loss_tensor = torch.tensor(total_loss, device=self.device)
                dist.all_reduce(total_loss_tensor, op=dist.ReduceOp.SUM)

                if self.is_main:
                    avg_loss = total_loss_tensor.item() / (
                        accum_steps * dist.get_world_size()
                    )
                    logger.info(
                        f"Epoch {self.state.epoch} Step {self.state.global_step} Loss: {avg_loss:.4f}"
                    )

                total_loss = 0.0
                accum_steps = 0

                clip_grad_norm_(self.model.parameters(), self.config.grad_clip_norm)
                self.optimizer.step()
                self.optimizer.zero_grad()
                self.scheduler.step()
                self.state.global_step += 1

        self.state.epoch += 1

    def validate(self):
        if not self.val_loader:
            return

        self.model.eval()
        total_loss = 0.0
        world_size = dist.get_world_size()

        with torch.no_grad():
            for batch in self.val_loader:
                batch = {k: v.to(self.device) for k, v in batch.items()}
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    loss, _ = self.model(
                        text_tokens=batch["text_tokens"],
                        audio_tokens=batch["audio_tokens"][:, :, :-1],
                        target=batch["audio_tokens"][:, :, 1:],
                        mask=batch["attention_mask"],
                    )

                loss_tensor = torch.tensor(loss.item(), device=self.device)
                dist.all_reduce(loss_tensor, op=dist.ReduceOp.SUM)
                total_loss += loss_tensor.item() / world_size

        avg_loss = total_loss / len(self.val_loader)

        if self.is_main:
            logger.info(f"Validation Loss: {avg_loss:.4f}")
            if avg_loss < self.state.best_loss:
                self.state.best_loss = avg_loss
                self.save_checkpoint(best=True)


def setup_distributed(config: TrainingConfig) -> torch.device:
    dist.init_process_group(backend=config.backend)
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    torch.manual_seed(config.seed + dist.get_rank())
    return torch.device(f"cuda:{local_rank}")


def train(config: TrainingConfig):
    device = setup_distributed(config)

    ttsTransformerArgs = TTSTransformerArgs()
    model = TTSTransformer(ttsTransformerArgs)

    trainer = DistributedTTSTrainer(config, model, device)

    start_epoch = trainer.state.epoch
    for epoch in range(start_epoch, config.epochs):
        trainer.train_epoch()

        if (epoch + 1) % config.validation_freq == 0:
            trainer.validate()

        if (epoch + 1) % config.checkpoint_freq == 0:
            trainer.save_checkpoint()

    dist.destroy_process_group()


if __name__ == "__main__":
    config = TrainingConfig(resume_checkpoint=None)
    train(config)
