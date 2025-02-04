import torch

from e3nn import o3
from e3nn.o3 import Irreps

from .sequential import Module
from .pointwise import PointwiseLinear
import torch
import torch.nn.functional

from torch import Tensor
from e3nn.util.jit import compile_mode
from typing import Optional, Dict, Tuple

import math

import torch

from torch import nn

from e3nn.math import soft_one_hot_linspace
from e3nn.util.jit import compile_mode
from ml_collections.config_dict import ConfigDict
from ..utils import build

@torch.jit.script
def symmetricCutoff(x: torch.Tensor, factor: float, p: float = 6.0) -> torch.Tensor:
    x = x * factor
    return (x-1)**2 *(x+1)**2 *(abs(x)<1.).float()

@torch.jit.script
def _poly_cutoff(x: torch.Tensor, factor: float, p: float = 6.0) -> torch.Tensor:
    x = x * factor

    out = 1.0
    out = out - (((p + 1.0) * (p + 2.0) / 2.0) * torch.pow(x, p))
    out = out + (p * (p + 2.0) * torch.pow(x, p + 1.0))
    out = out - ((p * (p + 1.0) / 2) * torch.pow(x, p + 2.0))

    return out * (x < 1.0)


class PolynomialCutoff(torch.nn.Module):
    _factor: float
    p: float

    def __init__(self, r_max: float, p: float = 6, cutoff=_poly_cutoff):
        r"""Polynomial cutoff, as proposed in DimeNet: https://arxiv.org/abs/2003.03123


        Parameters
        ----------
        r_max : float
            Cutoff radius

        p : int
            Power used in envelope function
        """
        super().__init__()
        assert p >= 2.0
        self.p = float(p)
        self._factor = 1.0 / float(r_max)
        self.cutoff = cutoff

    def forward(self, x):
        """
        Evaluate cutoff function.

        x: torch.Tensor, input distance
        """
        return self.cutoff(x, self._factor, p=self.p)


class BesselBasis(nn.Module):
    r_max: float
    prefactor: float

    def __init__(self, r_max, r_min=0, num_basis=8, trainable=True, one_over_r=True):
        r"""Radial Bessel Basis, as proposed in DimeNet: https://arxiv.org/abs/2003.03123


        Parameters
        ----------
        r_max : float
            Cutoff radius

        num_basis : int
            Number of Bessel Basis functions

        trainable : bool
            Train the :math:`n \pi` part or not.
            
        one_over_r:
            Set to true if the value should explode at x = 0, e.g. when x is the interatomic distance.
        """
        super(BesselBasis, self).__init__()

        self.trainable = trainable
        self.num_basis = num_basis

        self.r_max = float(r_max)
        self.r_min = float(r_min)
        self.prefactor = 2.0 / (self.r_max - self.r_min)
        self.one_over_r = one_over_r

        bessel_weights = (
            torch.linspace(start=1.0, end=num_basis, steps=num_basis) * math.pi
        )
        if self.trainable:
            self.bessel_weights = nn.Parameter(bessel_weights)
        else:
            self.register_buffer("bessel_weights", bessel_weights)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Evaluate Bessel Basis for input x.

        Parameters
        ----------
        x : torch.Tensor
            Input
        """
        numerator = torch.sin(self.bessel_weights * x.unsqueeze(-1) / (self.r_max - self.r_min))
        result = self.prefactor * numerator
        if self.one_over_r:
            result = result/x.unsqueeze(-1)
        return  result


@compile_mode("script")
class SphericalEncoding(Module):
    """Construct edge attrs as spherical harmonic projections of edge vectors.

    Parameters follow ``e3nn.o3.spherical_harmonics``.

    Args:
        irreps_edge_sh (int, str, or o3.Irreps): if int, will be treated as lmax for o3.Irreps.spherical_harmonics(lmax)
        edge_sh_normalization (str): the normalization scheme to use
        edge_sh_normalize (bool, default: True): whether to normalize the spherical harmonics
        out_field (str, default: AtomicDataDict.EDGE_ATTRS_KEY: data/irreps field
    """

    out_field: str

    def __init__(
        self,
        irreps_out,
        edge_sh_normalization: str = "component",
        edge_sh_normalize: bool = True,
        irreps_in="1x1o",
    ):
        super().__init__()
        self.init_irreps(
            vectors=irreps_in,
            spherical_harmonics=irreps_out,
            output_keys=["spherical_harmonics"],
        )
        self.mul = Irreps(self.irreps_in["vectors"])[0].mul
        irreps = []
        for irrep in Irreps(self.irreps_out["spherical_harmonics"]):
            assert irrep.mul == self.mul
            irreps += [str(irrep.ir)]
        self.sh = o3.SphericalHarmonics(
            "+".join(irreps), edge_sh_normalize, edge_sh_normalization
        )

    def forward(self, data: Dict[str, Tensor], attrs:Dict[str, Tuple[str, str]]):
        vectors = data["vectors"]
        cat, _ = vectors.shape
        edge_sh = self.sh(vectors.view(cat, self.mul, 3)).view(cat, -1)
        attrs = {
                    "spherical_harmonics": (
                        "edge",
                        self.irreps_out["spherical_harmonics"],
                    )
                }
        data = {"spherical_harmonics": edge_sh}
        return data, attrs


@compile_mode("script")
class RadialBasisEncoding(Module):
    def __init__(
        self,
        r_max,
        trainable,
        irreps_out,
        r_min=0,
        polynomial_degree=6,
        basis=BesselBasis,
        cutoff=_poly_cutoff,
        irreps_in="1x0e",
        one_over_r = True
    ):
        """
        Radial basis embedding of a real number.
        """
        super().__init__()
        self.init_irreps(
            input=irreps_in,
            radial_embedding=irreps_out,
            output_keys=["radial_embedding"],
        )
        num_basis = Irreps(self.irreps_out["radial_embedding"])
        num_basis = num_basis[0].mul
        self.basis = basis(r_max, r_min, num_basis, trainable, one_over_r=one_over_r)
        self.cutoff = PolynomialCutoff(r_max, p=polynomial_degree, cutoff=cutoff)
        self.r_max = r_max
        
    def forward(self, data: Dict[str, Tensor], attrs:Dict[str, Tuple[str, str]]):
        input = data['input']
        is_per = attrs['input'][0]
        embedded = (
            self.basis(input) * self.cutoff(input)[:, None]
        ).view(input.shape[0], -1)
        
        attrs = {"radial_embedding": (is_per, self.irreps_out["radial_embedding"])}
        data = {"radial_embedding": embedded}
        return data, attrs
          
          
@compile_mode("script")
class Broadcast(Module):
    def __init__(
        self,
        irreps_in,
        irreps_out,
        to
    ):
        """
        Broadcasts graph features to nodes or edges, or node features to edges.
        """
        super().__init__()
        self.init_irreps(
            input=irreps_in,
            output=irreps_out,
            output_keys=["output"],
        )
        self.to = to


    def forward(self, data: Dict[str, Tensor], attrs:Dict[str, Tuple[str, str]]):
        is_per = attrs['input'][0]
        input = data['input']

        assert is_per == 'graph'
        if self.to == 'node':
            output = input[data['_node_segment']]
        elif self.to == 'edge':
            output = input[data['_edge_segment']]
        else:
            raise ValueError()

        return {"output": output}, {"output": (self.to, self.irreps_out["output"])}


@compile_mode("script")
class OneHotEncoding(Module):
    num_types: int

    def __init__(
        self,
        num_types: int,
        irreps_out,
        irreps_in="0x0e",
    ):
        super().__init__()
        self.num_types = num_types
        self.init_irreps(input=irreps_in, one_hot=irreps_out, output_keys="one_hot")

    def forward(self, data: Dict[str, Tensor], attrs:Dict[str, Tuple[str, str]]):
        input = data["input"]
        type_numbers = input.squeeze(-1)

        one_hot = torch.nn.functional.one_hot(
            type_numbers, num_classes=self.num_types
        ).to(device=type_numbers.device, dtype=torch.float)

        attrs = {"one_hot": (attrs["input"][0], self.irreps_out["one_hot"])}
        data = {"one_hot": one_hot}
        return data, attrs
      
@compile_mode("script")
class RelativePositionEncoding(Module):
    def __init__(
        self,
        radial_encoding,
        segment,
        irreps_out,
        id = None
    ):
        super().__init__()
        self.init_irreps(input=segment, output=irreps_out, id=id, output_keys=['output'])
        radial_encoding['irreps_in'] = '1x0e'
        radial_encoding['irreps_out'] = self.irreps_out['output']
        radial_encoding = build(radial_encoding)
        self.radial = radial_encoding
        

    def forward(self, data: Dict[str, Tensor], attrs:Dict[str, Tuple[str, str]]):
        segment = data['input']
        if 'id' in self.irreps_in and not self.irreps_in['id'] is None:
            id = data['id']
            relative_pos = id[data['edge_index'][0]] - id[data['edge_index'][1]]
        else:
            relative_pos = data['edge_index'][0] - data['edge_index'][1]
        mask = segment[data['edge_index'][0]] == segment[data['edge_index'][1]]
        mask = mask.float()
        relative_pos = mask*relative_pos.view(-1, 1) + (1-mask)*1e5
        output, attrs = self.radial({'input':relative_pos}, attrs)

        return {'output':output['radial_embedding']}, {"output": ('edge', self.irreps_out["output"])}
