# SPDX-License-Identifier: Apache-2.0
"""Inference-only TorchSig v0.6.1 XCiT architecture.

Adapted from TorchDSP/torchsig, tagged v0.6.1, xcit/xcit1d.py.
The model-specific upstream license is retained in XCIT-LICENSE.txt.
Training/Lightning imports are intentionally excluded. Import only with the
optional ML dependencies installed; no weights are downloaded on import.
"""

import timm
import torch
from torch import Tensor, nn


class XCiT1d(nn.Module):
    """A 1D implementation of the XCiT architecture.

    Args:
        input_channels (int): Number of 1D input channels.
        n_features (int): Number of output features/classes.
        xcit_version (str): Version of XCiT model to use (e.g., 'nano_12_p16_224').
        drop_path_rate (float): Drop path rate for training.
        drop_rate (float): Dropout rate for training.
        ds_method (str): Downsampling method ('downsample' or 'chunk').
        ds_rate (int): Downsampling rate (e.g., 2 for downsampling by a factor of 2).
    """

    def __init__(
        self,
        input_channels: int,
        n_features: int,
        xcit_version: str = "nano_12_p16_224",
        drop_path_rate: float = 0.0,
        drop_rate: float = 0.3,
        ds_method: str = "downsample",
        ds_rate: int = 2,
    ):
        super().__init__()

        # Ensure the model name is correct
        model_name = (
            f"xcit_{xcit_version}" if not xcit_version.startswith("xcit_") else xcit_version
        )

        # Create the backbone model
        self.backbone = timm.create_model(
            model_name,
            pretrained=False,
            num_classes=n_features,
            in_chans=input_channels,
            drop_path_rate=drop_path_rate,
            drop_rate=drop_rate,
        )

        # Number of features from the backbone
        W = self.backbone.num_features

        # Include the grouper Conv1d layer
        self.grouper = nn.Conv1d(W, n_features, kernel_size=1)

        # Replace the patch embedding with a 1D version
        if ds_method == "downsample":
            self.backbone.patch_embed = ConvDownSampler(input_channels, W, ds_rate)
        elif ds_method == "chunk":
            self.backbone.patch_embed = Chunker(input_channels, W, ds_rate)
        else:
            raise ValueError(
                f"{ds_method} is unsupported; choose downsample or chunk"
            )

        # Replace the classifier head with an identity layer (since we use self.grouper)
        self.backbone.head = nn.Identity()

    def forward(self, x: Tensor) -> Tensor:
        mdl = self.backbone
        B = x.shape[0]

        # Patch embedding
        x = self.backbone.patch_embed(x)  # Shape: [B, C, L]

        # Define H and W for 1D data
        Hp, Wp = x.shape[-1], 1  # Height is sequence length, Width is 1

        # Obtain positional encoding
        pos_encoding = mdl.pos_embed(B, Hp, Wp).reshape(B, -1, Hp).permute(0, 2, 1)

        # Add positional encoding
        x = x.transpose(1, 2) + pos_encoding  # Shape: [B, Hp, C]

        # Apply transformer blocks
        for blk in mdl.blocks:
            x = blk(x, Hp, Wp)

        # Classification token
        cls_tokens = mdl.cls_token.expand(B, -1, -1)  # Shape: [B, 1, C]
        x = torch.cat((cls_tokens, x), dim=1)  # Shape: [B, Hp+1, C]

        # Apply class attention blocks
        for blk in mdl.cls_attn_blocks:
            x = blk(x)

        # Layer normalization
        x = mdl.norm(x)  # Shape: [B, Hp+1, C]

        # Apply the grouper Conv1d to the classification token
        # Extract the classification token (first token)
        cls_token = x[:, 0, :]  # Shape: [B, C]

        # Reshape for Conv1d: [B, C, 1]
        cls_token = cls_token.unsqueeze(-1)  # Shape: [B, C, 1]

        # Apply the grouper Conv1d
        x = self.grouper(cls_token).squeeze(-1)  # Shape: [B, n_features]

        # If x is 1D (batch size 1), ensure it has the correct shape
        if x.dim() == 1:
            x = x.unsqueeze(0)

        return x


class ConvDownSampler(nn.Module):
    def __init__(self, in_chans: int, embed_dim: int, ds_rate: int = 16):
        super().__init__()
        # Use a single convolutional layer with appropriate stride
        self.conv = nn.Conv1d(
            in_channels=in_chans,
            out_channels=embed_dim,
            kernel_size=ds_rate * 2,
            stride=ds_rate,
            padding=ds_rate // 2,
        )
        self.bn = nn.BatchNorm1d(embed_dim)
        self.act = nn.GELU()

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(x)
        x = self.bn(x)
        x = self.act(x)
        return x


class Chunker(nn.Module):
    def __init__(self, in_chans: int, embed_dim: int, ds_rate: int = 16):
        super().__init__()
        self.ds_rate = ds_rate
        self.embed = nn.Conv1d(in_chans, embed_dim, kernel_size=7, padding=3)
        self.pool = nn.AvgPool1d(kernel_size=ds_rate, stride=ds_rate)

    def forward(self, x: Tensor) -> Tensor:
        x = self.embed(x)  # Shape: [B, embed_dim, L]
        x = self.pool(x)  # Downsample by averaging
        return x
