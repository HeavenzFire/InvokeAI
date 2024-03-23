# copied from https://github.com/tencent-ailab/IP-Adapter (Apache License 2.0)
#   and modified as needed

from typing import List, Optional, TypedDict, Union

import torch
from PIL import Image
from safetensors import safe_open
from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection

from invokeai.backend.ip_adapter.ip_attention_weights import IPAttentionWeights

from ..raw_model import RawModel
from .resampler import Resampler


class IPAdapterStateDict(TypedDict):
    ip_adapter: dict[str, torch.Tensor]
    image_proj: dict[str, torch.Tensor]


class ImageProjModel(torch.nn.Module):
    """Image Projection Model"""

    def __init__(
        self, cross_attention_dim: int = 1024, clip_embeddings_dim: int = 1024, clip_extra_context_tokens: int = 4
    ):
        super().__init__()

        self.cross_attention_dim = cross_attention_dim
        self.clip_extra_context_tokens = clip_extra_context_tokens
        self.proj = torch.nn.Linear(clip_embeddings_dim, self.clip_extra_context_tokens * cross_attention_dim)
        self.norm = torch.nn.LayerNorm(cross_attention_dim)

    @classmethod
    def from_state_dict(cls, state_dict: dict[str, torch.Tensor], clip_extra_context_tokens: int = 4):
        """Initialize an ImageProjModel from a state_dict.

        The cross_attention_dim and clip_embeddings_dim are inferred from the shape of the tensors in the state_dict.

        Args:
            state_dict (dict[torch.Tensor]): The state_dict of model weights.
            clip_extra_context_tokens (int, optional): Defaults to 4.

        Returns:
            ImageProjModel
        """
        cross_attention_dim = state_dict["norm.weight"].shape[0]
        clip_embeddings_dim = state_dict["proj.weight"].shape[-1]

        model = cls(cross_attention_dim, clip_embeddings_dim, clip_extra_context_tokens)

        model.load_state_dict(state_dict)
        return model

    def forward(self, image_embeds):
        embeds = image_embeds
        clip_extra_context_tokens = self.proj(embeds).reshape(
            -1, self.clip_extra_context_tokens, self.cross_attention_dim
        )
        clip_extra_context_tokens = self.norm(clip_extra_context_tokens)
        return clip_extra_context_tokens


class MLPProjModel(torch.nn.Module):
    """SD model with image prompt"""

    def __init__(self, cross_attention_dim: int = 1024, clip_embeddings_dim: int = 1024):
        super().__init__()

        self.proj = torch.nn.Sequential(
            torch.nn.Linear(clip_embeddings_dim, clip_embeddings_dim),
            torch.nn.GELU(),
            torch.nn.Linear(clip_embeddings_dim, cross_attention_dim),
            torch.nn.LayerNorm(cross_attention_dim),
        )

    @classmethod
    def from_state_dict(cls, state_dict: dict[str, torch.Tensor]):
        """Initialize an MLPProjModel from a state_dict.

        The cross_attention_dim and clip_embeddings_dim are inferred from the shape of the tensors in the state_dict.

        Args:
            state_dict (dict[torch.Tensor]): The state_dict of model weights.

        Returns:
            MLPProjModel
        """
        cross_attention_dim = state_dict["proj.3.weight"].shape[0]
        clip_embeddings_dim = state_dict["proj.0.weight"].shape[0]

        model = cls(cross_attention_dim, clip_embeddings_dim)

        model.load_state_dict(state_dict)
        return model

    def forward(self, image_embeds):
        clip_extra_context_tokens = self.proj(image_embeds)
        return clip_extra_context_tokens


class IPAdapter(RawModel):
    """IP-Adapter: https://arxiv.org/pdf/2308.06721.pdf"""

    def __init__(
        self,
        state_dict: IPAdapterStateDict,
        device: torch.device,
        dtype: torch.dtype = torch.float16,
        num_tokens: int = 4,
    ):
        self.device = device
        self.dtype = dtype

        self._num_tokens = num_tokens

        self._clip_image_processor = CLIPImageProcessor()

        self._image_proj_model = self._init_image_proj_model(state_dict["image_proj"])

        self.attn_weights = IPAttentionWeights.from_state_dict(state_dict["ip_adapter"]).to(
            self.device, dtype=self.dtype
        )

    def to(self, device: torch.device, dtype: Optional[torch.dtype] = None):
        self.device = device
        if dtype is not None:
            self.dtype = dtype

        self._image_proj_model.to(device=self.device, dtype=self.dtype)
        self.attn_weights.to(device=self.device, dtype=self.dtype)

    def calc_size(self):
        # workaround for circular import
        from invokeai.backend.model_manager.load.model_util import calc_model_size_by_data

        return calc_model_size_by_data(self._image_proj_model) + calc_model_size_by_data(self.attn_weights)

    def _init_image_proj_model(self, state_dict: dict[str, torch.Tensor]):
        return ImageProjModel.from_state_dict(state_dict, self._num_tokens).to(self.device, dtype=self.dtype)

    @torch.inference_mode()
    def get_image_embeds(self, pil_image: List[Image.Image], image_encoder: CLIPVisionModelWithProjection):
        clip_image = self._clip_image_processor(images=pil_image, return_tensors="pt").pixel_values
        clip_image_embeds = image_encoder(clip_image.to(self.device, dtype=self.dtype)).image_embeds
        image_prompt_embeds = self._image_proj_model(clip_image_embeds)
        uncond_image_prompt_embeds = self._image_proj_model(torch.zeros_like(clip_image_embeds))
        return image_prompt_embeds, uncond_image_prompt_embeds


class IPAdapterPlus(IPAdapter):
    """IP-Adapter with fine-grained features"""

    def _init_image_proj_model(self, state_dict: dict[str, torch.Tensor]):
        return Resampler.from_state_dict(
            state_dict=state_dict,
            depth=4,
            dim_head=64,
            heads=12,
            num_queries=self._num_tokens,
            ff_mult=4,
        ).to(self.device, dtype=self.dtype)

    @torch.inference_mode()
    def get_image_embeds(self, pil_image: List[Image.Image], image_encoder: CLIPVisionModelWithProjection):
        clip_image = self._clip_image_processor(images=pil_image, return_tensors="pt").pixel_values
        clip_image = clip_image.to(self.device, dtype=self.dtype)
        clip_image_embeds = image_encoder(clip_image, output_hidden_states=True).hidden_states[-2]
        image_prompt_embeds = self._image_proj_model(clip_image_embeds)
        uncond_clip_image_embeds = image_encoder(torch.zeros_like(clip_image), output_hidden_states=True).hidden_states[
            -2
        ]
        uncond_image_prompt_embeds = self._image_proj_model(uncond_clip_image_embeds)
        return image_prompt_embeds, uncond_image_prompt_embeds


class IPAdapterFull(IPAdapterPlus):
    """IP-Adapter Plus with full features."""

    def _init_image_proj_model(self, state_dict: dict[str, torch.Tensor]):
        return MLPProjModel.from_state_dict(state_dict).to(self.device, dtype=self.dtype)


class IPAdapterPlusXL(IPAdapterPlus):
    """IP-Adapter Plus for SDXL."""

    def _init_image_proj_model(self, state_dict: dict[str, torch.Tensor]):
        return Resampler.from_state_dict(
            state_dict=state_dict,
            depth=4,
            dim_head=64,
            heads=20,
            num_queries=self._num_tokens,
            ff_mult=4,
        ).to(self.device, dtype=self.dtype)


def build_ip_adapter(
    ip_adapter_ckpt_path: str, device: torch.device, dtype: torch.dtype = torch.float16
) -> Union[IPAdapter, IPAdapterPlus]:
    state_dict: IPAdapterStateDict = {"ip_adapter": {}, "image_proj": {}}

    if ip_adapter_ckpt_path.endswith("safetensors"):
        state_dict = {"ip_adapter": {}, "image_proj": {}}
        model = safe_open(ip_adapter_ckpt_path, device=device.type, framework="pt")
        for key in model.keys():
            if key.startswith("image_proj."):
                state_dict["image_proj"][key.replace("image_proj.", "")] = model.get_tensor(key)
            if key.startswith("ip_adapter."):
                state_dict["ip_adapter"][key.replace("ip_adapter.", "")] = model.get_tensor(key)
    else:
        ip_adapter_diffusers_checkpoint_path = ip_adapter_ckpt_path + "/ip_adapter.bin"
        state_dict = torch.load(ip_adapter_diffusers_checkpoint_path, map_location="cpu")

    if "proj.weight" in state_dict["image_proj"]:  # IPAdapter (with ImageProjModel).
        return IPAdapter(state_dict, device=device, dtype=dtype)
    elif "proj_in.weight" in state_dict["image_proj"]:  # IPAdaterPlus or IPAdapterPlusXL (with Resampler).
        cross_attention_dim = state_dict["ip_adapter"]["1.to_k_ip.weight"].shape[-1]
        if cross_attention_dim == 768:
            # SD1 IP-Adapter Plus
            return IPAdapterPlus(state_dict, device=device, dtype=dtype)
        elif cross_attention_dim == 2048:
            # SDXL IP-Adapter Plus
            return IPAdapterPlusXL(state_dict, device=device, dtype=dtype)
        else:
            raise Exception(f"Unsupported IP-Adapter Plus cross-attention dimension: {cross_attention_dim}.")
    elif "proj.0.weight" in state_dict["image_proj"]:  # IPAdapterFull (with MLPProjModel).
        return IPAdapterFull(state_dict, device=device, dtype=dtype)
    else:
        raise ValueError(f"'{ip_adapter_ckpt_path}' has an unrecognized IP-Adapter model architecture.")
