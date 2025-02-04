from functools import partial
from ..data import computeEdgeIndex
from ml_collections.config_dict import ConfigDict
import ase
from .layer_configs import featureModel
from ..nn import PointwiseLinear


def get_config(spec=None):
    config = ConfigDict()
    data, model = ConfigDict(), ConfigDict()
    config.data_config = data
    config.model_config = model

    config.epoch_subdivision = 1
    config.learning_rate = 1e-2
    config.batch_size = 256

    config.use_ema = True
    config.ema_decay = 0.99
    config.ema_use_num_updates = True
    config.metric_key = "validation_loss"  # saves the best model according to this

    config.max_epochs = int(1e6)
    config.early_stopping_patiences = {"validation_loss": 20}
    config.early_stopping_lower_bounds = {"LR": 1e-6}

    config.loss_coeffs = {"monopole": [1e3, "MSELoss"], "dipole": [1e3, "MSELoss"], "quadrupole_2":[1e3, "MSELoss"]}
    config.metrics_components = {"monopole": ["mae"], "dipole": ["mae"], "quadrupole_2":["mae"]}
    config.optimizer_name = "Adam"
    config.lr_scheduler_name = "ReduceLROnPlateau"
    config.lr_scheduler_patience = 2
    config.lr_scheduler_factor = 0.8

    model.n_dim = 32
    model.l_max = 2
    model.r_max = 5.0
    model.num_layers = 5
    model.node_attrs = "16x0e"
    model.jit = True
    num_types = 18

    data.n_train = 66105
    data.n_val = 7345
    data.train_val_split = "random"
    data.shuffle = True
    data.path = "data/dmff_e3nn_infer/alldata/quadrupole/data.hdf5"
    data.type_names = list(ase.atom.atomic_numbers.keys())[:num_types]
    data.preprocess = [partial(computeEdgeIndex, r_max=model.r_max, r_min=0.5)]
    
    if spec:
        override = eval(spec)
        config.update_from_flattened_dict(override)

    features = "+".join(
        [f"{model.n_dim}x{n}e+{model.n_dim}x{n}o" for n in range(model.l_max + 1)]
    )

    edge_spherical = "1x0e+1x1o+1x2e"
    layer_configs = featureModel(
        n_dim=model.n_dim,
        l_max=model.l_max,
        edge_spherical=edge_spherical,
        node_attrs=model.node_attrs,
        edge_radial="8x0e",
        num_types=num_types,
        num_layers=model.num_layers,
        r_max=model.r_max,
    )
    layer_configs.layers.append(
        (
            "monopole_output",
            {
                "module": PointwiseLinear,
                "irreps_in": (features, "node_features"),
                "irreps_out": (f"1x0e", "monopole"),
            },
        )
    )
    layer_configs.layers.append(
        (
            "dipole_output",
            {
                "module": PointwiseLinear,
                "irreps_in": (features, "node_features"),
                "irreps_out": (f"1x1o", "dipole"),
            },
        )
    )
    layer_configs.layers.append(
        (
            "quadrupole_2_output",
            {
                "module": PointwiseLinear,
                "irreps_in": (features, "node_features"),
                "irreps_out": (f"1x2e", "quadrupole_2"),
            },
        )
    )
    model.update(layer_configs)

    return config
