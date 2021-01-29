import pyro
import pyro.distributions as dist
import torch
import torch.nn as nn
from pyro.infer.autoguide import AutoDiagonalNormal
from pyro.nn import PyroModule

from scvi import _CONSTANTS
from scvi.compose import PyroBaseModuleClass
from scvi.data import synthetic_iid
from scvi.dataloaders import AnnDataLoader
from scvi.lightning import PyroTrainingPlan, Trainer


class BayesianRegression(PyroModule, PyroBaseModuleClass):
    def __init__(self, in_features, out_features):
        super().__init__()

        self._auto_guide = AutoDiagonalNormal(self)

        self.register_buffer("zero", torch.tensor(0.0, requires_grad=False))
        self.register_buffer("one", torch.tensor(1.0, requires_grad=False))
        self.register_buffer("ten", torch.tensor(10.0, requires_grad=False))

        self.linear = nn.Linear(in_features, out_features)

    def _get_forward_tensors(self, tensors):
        x = tensors[_CONSTANTS.X_KEY]
        y = tensors[_CONSTANTS.LABELS_KEY]

        return x, y

    def _forward(self, x, y):
        sigma = pyro.sample("sigma", dist.Uniform(self.zero, self.ten))
        mean = self.linear(x).squeeze(-1)
        with pyro.plate("data", x.shape[0]):
            pyro.sample("obs", dist.Normal(mean, sigma), obs=y)
        return mean

    def _get_guide_tensors(self, tensors):
        return [tensors]

    def _guide(self, tensors):
        return self._auto_guide(tensors)


def test_pyro_bayesian_regression():
    use_gpu = int(torch.cuda.is_available())
    adata = synthetic_iid()
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    pyro.clear_param_store()
    model = BayesianRegression(adata.shape[1], 1)
    plan = PyroTrainingPlan(model)
    trainer = Trainer(
        gpus=use_gpu,
        max_epochs=2,
    )
    trainer.fit(plan, train_dl)


def test_pyro_bayesian_regression_jit():
    use_gpu = int(torch.cuda.is_available())
    adata = synthetic_iid()
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    pyro.clear_param_store()
    model = BayesianRegression(adata.shape[1], 1)
    # warmup guide for JIT
    for tensors in train_dl:
        model.guide(tensors)
        break
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    plan = PyroTrainingPlan(model, loss_fn=pyro.infer.JitTrace_ELBO())
    trainer = Trainer(
        gpus=use_gpu,
        max_epochs=2,
    )
    trainer.fit(plan, train_dl)