import torch
from .data import Data
import warnings
import numpy as np
import ase.neighborlist
import ase
from tqdm import tqdm, trange

from torch import Tensor
from e3nn.util.jit import compile_mode
from typing import Optional, Dict, Tuple

def computeEdgeVector(data: Dict[str, Tensor], attrs:Dict[str, Tuple[str, str]], with_lengths: bool = True):
    """Compute the edge displacement vectors for a graph.

    If ``data.pos.requires_grad`` and/or ``data.cell.requires_grad``, this
    method will return edge vectors correctly connected in the autograd graph.

    Returns:
        Tensor [n_edges, 3] edge displacement vectors
    """
    attrs["edge_vector"] = ("edge", "1x1o")
    attrs["edge_length"] = ("edge", "1x0e")
    if "edge_vector" in data.keys():
        if with_lengths and "edge_length" not in data.keys():
            data["edge_length"] = torch.linalg.norm(data["edge_vector"], dim=-1)
        return data, attrs
    else:
        pos = data["pos"]
        edge_index = data["edge_index"]
        edge_vec = pos[edge_index[1]] - pos[edge_index[0]]
        data["edge_vector"] = edge_vec
        if with_lengths:
            data["edge_length"] = torch.linalg.norm(edge_vec, dim=-1)
        return data, attrs

@torch.jit.ignore
def computeEdgeIndex(
    data,
    attrs,
    r_max: float = None,
    criteria = None
):
    """
    Compute edge indices between nodes within r_max.
    If there has already been an edge_index in batch, map all edge features to be consistent with the new edge indices.
    Zero-pad the features for new edges.
    """

    pos = data["pos"]
    pos = torch.as_tensor(pos, dtype=torch.get_default_dtype())        
    
    # per graph fully connected
    edge_index_lst = []
    cnt = 0
    for n_nodes in data['_n_nodes']:
        n_nodes = n_nodes.item()
        edge_matrix = torch.zeros((n_nodes, n_nodes), dtype=torch.long)
        edge_matrix += torch.arange(cnt, cnt+n_nodes)
        edge_index = torch.stack([edge_matrix.permute(1, 0).reshape(-1), edge_matrix.reshape(-1)])
        edge_index_lst.append(edge_index)
        cnt += n_nodes
    edge_index = torch.cat(edge_index_lst, dim=1).to(pos.device)
    
    # filter edges according to distance
    distance = pos[edge_index[0]] - pos[edge_index[1]]
    distance = torch.linalg.norm(distance, dim=-1)
    mask =  distance < r_max
    
    # filter edges according to custom criteria
    if not criteria is None:
        mask = torch.logical_or(mask, criteria(data, edge_index))
    mask = torch.logical_and(mask, torch.logical_not(edge_index[0]==edge_index[1]))

    def computeEdgeMap(a, b):
        j = 0
        lst = []
        for i in range(a.shape[1]):
            while not (b[:, j] == a[:, i]).all():
                j += 1
            lst.append(j)
        return torch.tensor(lst)
      
    if 'edge_index' in data:
        edge_map = computeEdgeMap(data['edge_index'], edge_index)
        mask[edge_map] = True
        
    mask = mask.expand((2, -1))
    edge_index = edge_index[mask].reshape(2, -1)
    
    # map edge attributes to new edge indices
    if 'edge_index' in data:
        edge_map = computeEdgeMap(data['edge_index'], edge_index)
        for key in attrs:
            if attrs[key][0] == 'edge': 
                tmp = data[key]
                data[key] = torch.zeros(edge_index.shape[1], data[key].shape[1], dtype=data[key].dtype).to(pos.device)
                data[key][edge_map] = tmp
                    
    #if '_node_segment' in data:
    #    n_edges = torch.bincount(data['_node_segment'][edge_index[0]]).view(-1, 1)
    #else:
    #    n_edges = torch.ones((1,), dtype=torch.long) * edge_index.shape[1]

    attrs["_n_edges"] = ('graph', '1x0e')
    #data["_n_edges"] = n_edges
    
    data = {}
    data["edge_index"] = edge_index
    return data, attrs
