from dataclasses import dataclass

import torch
from einops import einsum, rearrange
from jaxtyping import Float
from torch import Tensor, nn

from ....geometry.projection import get_world_rays
from ....misc.sh_rotation import rotate_sh
from .gaussians import (
    build_covariance, 
    matrix_to_quaternion, 
    quaternion_to_matrix,
    quaternion_raw_multiply
)



@dataclass
class Gaussians:
    means: Float[Tensor, "*batch 3"]
    covariances: Float[Tensor, "*batch 3 3"]
    scales: Float[Tensor, "*batch 3"]
    rotations: Float[Tensor, "*batch 4"]
    harmonics: Float[Tensor, "*batch 3 _"]
    opacities: Float[Tensor, " *batch"]

@dataclass
class GaussianAdapterCfg:
    gaussian_scale_min: float
    gaussian_scale_max: float
    sh_degree: int


class GaussianAdapter(nn.Module):
    cfg: GaussianAdapterCfg

    def __init__(self, cfg: GaussianAdapterCfg):
        super().__init__()
        self.cfg = cfg

        # Create a mask for the spherical harmonics coefficients. This ensures that at
        # initialization, the coefficients are biased towards having a large DC
        # component and small view-dependent components.
        self.register_buffer(
            "sh_mask",
            torch.ones((self.d_sh,), dtype=torch.float32),
            persistent=False,
        )
        for degree in range(1, self.cfg.sh_degree + 1):
            self.sh_mask[degree**2 : (degree + 1) ** 2] = 0.1 * 0.25**degree
        self.init_sh_transform_matrices()

    def init_sh_transform_matrices(self):
        v_to_sh_transform = torch.tensor([[ 0, 0,-1],
                                          [-1, 0, 0],
                                          [ 0, 1, 0]], dtype=torch.float32)
        sh_to_v_transform = v_to_sh_transform.transpose(0, 1)
        self.register_buffer('sh_to_v_transform', sh_to_v_transform.unsqueeze(0))
        self.register_buffer('v_to_sh_transform', v_to_sh_transform.unsqueeze(0))

    def transform_SHs(self, shs, source_cameras_to_world):
        # shs: B x N x SH_num x 3
        # source_cameras_to_world: B 4 4
        # assert shs.shape[2] == 3, "Can only process shs order 1"
        b, n, r = shs.shape[:3]

        extrinsics = rearrange(source_cameras_to_world, "b v x y z i j -> (b v x y z) i j")
        shs = rearrange(shs, "b n r i j rgb sh_num -> (b n) (r rgb i j) sh_num")
        # shs_ = rearrange(shs_, 'b n rgb sh_num -> b (n rgb) sh_num')
        # shs = rearrange(shs, 'b n sh_num rgb -> b (n rgb) sh_num')
        transforms = torch.bmm(
            self.sh_to_v_transform.expand(extrinsics.shape[0], 3, 3),
            # transpose is because source_cameras_to_world is
            # in row major order 
            extrinsics.transpose(-1, -2))
        transforms = torch.bmm(transforms, 
            self.v_to_sh_transform.expand(extrinsics.shape[0], 3, 3))
        
        shs_transformed = torch.bmm(shs, transforms)
        shs_transformed = rearrange(shs_transformed, '(b n) (r rgb) sh_num -> b n r () () rgb sh_num', rgb=3, b=b, n=n, r=r)

        return shs_transformed

    def forward(
        self,
        extrinsics: Float[Tensor, "*#batch 4 4"],
        intrinsics: Float[Tensor, "*#batch 3 3"],
        coordinates: Float[Tensor, "*#batch 2"],
        depths: Float[Tensor, "*#batch"],
        opacities: Float[Tensor, "*#batch"],
        raw_gaussians: Float[Tensor, "*#batch _"],
        image_shape: tuple[int, int],
        input_images: Tensor | None = None,        
        eps: float = 1e-8,
    ) -> Gaussians:
        device = extrinsics.device
        scales, rotations, sh = raw_gaussians.split((3, 4, 3 * self.d_sh), dim=-1)

        # Map scale features to valid scale range.
        scale_min = self.cfg.gaussian_scale_min
        scale_max = self.cfg.gaussian_scale_max
        scales = scale_min + (scale_max - scale_min) * scales.sigmoid()
        h, w = image_shape
        pixel_size = 1 / torch.tensor((w, h), dtype=torch.float32, device=device)
        multiplier = self.get_scale_multiplier(intrinsics, pixel_size)
        scales = scales * depths[..., None] * multiplier[..., None]

        # Normalize the quaternion features to yield a valid quaternion.
        rotations = rotations / (rotations.norm(dim=-1, keepdim=True) + eps)

        sh = rearrange(sh, "... (xyz d_sh) -> ... xyz d_sh", xyz=3)
        sh = sh.broadcast_to((*opacities.shape, 3, self.d_sh)) * self.sh_mask
        
        if input_images is not None:
            # [B, V, H*W, 1, 1, 3]
            imgs = rearrange(input_images, "b v c h w -> b v (h w) () () c")
            # init sh with input images
            sh[..., 0] = sh[..., 0] + RGB2SH(imgs)

        # Create world-space covariance matrices.
        covariances = build_covariance(scales, rotations)
        c2w_rotations = extrinsics[..., :3, :3]
        covariances = c2w_rotations @ covariances @ c2w_rotations.transpose(-1, -2)

        sh_dc = sh[..., :3, :1]
        sh_feat = sh[..., :3, 1:]
        sh_feat = self.transform_SHs(sh_feat, c2w_rotations)
        shs = torch.cat((sh_dc, sh_feat), dim=-1)

        # Compute Gaussian means.
        origins, directions = get_world_rays(coordinates, extrinsics, intrinsics)
        means = origins + directions * depths[..., None]

        # shs = rotate_sh(sh, c2w_clone[..., None, :, :])

        return Gaussians(
            means=means,
            covariances=covariances,
            harmonics=shs,
            opacities=opacities,
            # Note: These aren't yet rotated into world space, but they're only used for
            # exporting Gaussians to ply files. This needs to be fixed...
            scales=scales,
            rotations=rotations.broadcast_to((*scales.shape[:-1], 4)),
        )

    def get_scale_multiplier(
        self,
        intrinsics: Float[Tensor, "*#batch 3 3"],
        pixel_size: Float[Tensor, "*#batch 2"],
        multiplier: float = 0.1,
    ) -> Float[Tensor, " *batch"]:
        xy_multipliers = multiplier * einsum(
            intrinsics[..., :2, :2].inverse(),
            pixel_size,
            "... i j, j -> ... i",
        )
        return xy_multipliers.sum(dim=-1)

    @property
    def d_sh(self) -> int:
        return (self.cfg.sh_degree + 1) ** 2

    @property
    def d_in(self) -> int:
        return 7 + 3 * self.d_sh

def RGB2SH(rgb):
    C0 = 0.28209479177387814
    return (rgb - 0.5) / C0