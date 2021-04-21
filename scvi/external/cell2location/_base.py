from typing import Optional

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pyro
import torch
from pyro.infer import SVI, Predictive
from tqdm.auto import tqdm

from scvi.dataloaders import AnnDataLoader
from scvi.model._utils import parse_use_gpu_arg
from scvi.train import PyroTrainingPlan, Trainer


class Cell2locationTrainSampleMixin:
    """
    Reimplementation of cell2location [Kleshchevnikov20]_ model. This mixin class provides useful methods.

    https://github.com/BayraktarLab/cell2location

    Parameters
    ----------
    sc_adata
        single-cell AnnData object that has been registered via :func:`~scvi.data.setup_anndata`.
    use_gpu
        Use the GPU or not.
    **model_kwargs
        Keyword args for :class:`~scvi.external.cell2location...`

    Examples
    --------
    >>>
    """

    @property
    def _plan_class(self):
        return PyroTrainingPlan

    def _train_full_data(self, max_epochs, use_gpu, plan_kwargs):

        args, kwargs = self.module.model._get_fn_args_full_data(self.adata)
        gpus, device = parse_use_gpu_arg(use_gpu)

        args = [a.to(device) for a in args]
        kwargs = {k: v.to(device) for k, v in kwargs.items()}
        self.to_device(device)

        pyro.clear_param_store()
        self.module.guide(*args, **kwargs)

        svi = SVI(
            self.module.model,
            self.module.guide,
            plan_kwargs["optim"],
            loss=plan_kwargs["loss_fn"],
        )

        iter_iterator = tqdm(range(max_epochs))
        hist = []
        for it in iter_iterator:

            loss = svi.step(*args, **kwargs)
            iter_iterator.set_description(
                "Epoch " + "{:d}".format(it) + ", -ELBO: " + "{:.4e}".format(loss)
            )
            hist.append(loss)

            if it % 500 == 0:
                torch.cuda.empty_cache()

        self.module.history_ = hist

    def _train_minibatch(
        self,
        max_epochs,
        use_gpu,
        plan_kwargs,
        trainer_kwargs,
        early_stopping: bool = False,
    ):

        pyro.clear_param_store()
        gpus, device = parse_use_gpu_arg(use_gpu)
        if max_epochs is None:
            n_obs = self.adata.n_obs
            max_epochs = np.min([round((20000 / n_obs) * 400), 400])

        plan_kwargs = plan_kwargs if isinstance(plan_kwargs, dict) else dict()
        trainer_kwargs = trainer_kwargs if isinstance(trainer_kwargs, dict) else dict()

        batch_size = self.module.model.batch_size
        train_dl = AnnDataLoader(self.adata, shuffle=True, batch_size=batch_size)

        plan = PyroTrainingPlan(
            self.module, **plan_kwargs  # n_obs=len(train_dl.indices),
        )
        es = "early_stopping"
        trainer_kwargs[es] = (
            early_stopping if es not in trainer_kwargs.keys() else trainer_kwargs[es]
        )
        trainer = Trainer(gpus=gpus, max_epochs=max_epochs, **trainer_kwargs)
        trainer.fit(plan, train_dl)
        self.module.to(device)

        try:
            self.module.history_ = trainer.logger.history
        except AttributeError:
            self.history_ = None

    def train(
        self,
        max_epochs: Optional[int] = None,
        use_gpu: Optional[bool] = None,
        train_size: float = 1,
        validation_size: Optional[float] = None,
        lr: float = 0.005,
        clip_norm: float = 200,
        trainer_kwargs: Optional[dict] = None,
        early_stopping: bool = False,
    ):

        plan_kwargs = {
            "loss_fn": pyro.infer.Trace_ELBO(),
            "optim": pyro.optim.ClippedAdam(
                {
                    "lr": lr,
                    # limit the gradient step from becoming too large
                    "clip_norm": clip_norm,
                }
            ),
        }

        batch_size = self.module.model.batch_size

        if batch_size is None:
            # train using full data (faster for small datasets)
            self._train_full_data(
                max_epochs=max_epochs, use_gpu=use_gpu, plan_kwargs=plan_kwargs
            )
        else:
            # standard training using minibatches
            self._train_minibatch(
                max_epochs=max_epochs,
                use_gpu=use_gpu,
                plan_kwargs=plan_kwargs,
                trainer_kwargs=trainer_kwargs,
                early_stopping=early_stopping,
            )

    def _sample_node(self, node, num_samples_batch: int = 10):

        self.module.batch_size = self.adata.n_obs

        args, kwargs = self.module._get_fn_args_for_predictive(self.adata)

        if self.use_gpu is True:
            self.module.cuda()

        predictive = Predictive(
            self.module.model, guide=self.module.guide, num_samples=num_samples_batch
        )

        post_samples = {
            k: v.detach().cpu().numpy()
            for k, v in predictive(*args, **kwargs).items()
            if k == node
        }

        return post_samples[node]

    def sample_node(self, node, n_sampl_batches, num_samples_batch: int = 10, suff=""):

        # sample first batch
        self.samples[node + suff] = self._sample_node(
            node, num_samples_batch=num_samples_batch
        )

        for it in tqdm(range(n_sampl_batches - 1)):
            # sample remaining batches
            post_node = self._sample_node(node, num_samples_batch=num_samples_batch)

            # concatenate batches
            self.samples[node + suff] = np.concatenate(
                (self.samples[node + suff], post_node), axis=0
            )

        # compute mean across samples
        self.samples[node + suff] = self.samples[node + suff].mean(0)

    def _sample_all(self, num_samples_batch: int = 10):

        self.module.batch_size = self.adata.n_obs

        args, kwargs = self.module._get_fn_args_for_predictive(self.adata)

        if self.use_gpu is True:
            self.module.cuda()

        predictive = Predictive(
            self.module.model, guide=self.module.guide, num_samples=num_samples_batch
        )

        post_samples = {
            k: v.detach().cpu().numpy() for k, v in predictive(*args, **kwargs).items()
        }

        return post_samples

    def sample_all(self, n_sampl_batches, num_samples_batch: int = 10):

        self.adata.uns["mod"] = {}

        # sample first batch
        self.adata.uns["mod"]["post_samples"] = self._sample_all(
            num_samples_batch=num_samples_batch
        )

        for it in tqdm(range(n_sampl_batches - 1)):
            # sample remaining batches
            post_samples = self._sample_all(num_samples_batch=num_samples_batch)

            # concatenate batches
            self.adata.uns["mod"]["post_samples"] = {
                k: np.concatenate(
                    (self.adata.uns["mod"]["post_samples"][k], post_samples[k]), axis=0
                )
                for k in post_samples.keys()
            }

    def sample_posterior(
        self,
        node="all",
        n_samples: int = 1000,
        num_samples_batch: int = 10,
        save_samples=False,
    ):
        r"""Sample posterior distribution of parameters - either all or single parameter
        :param node: pyro parameter to sample (e.g. default "all", self.spot_factors)
        :param n_samples: number of posterior samples to generate (1000 is recommended, reduce if you get GPU memory error)
        :param save_samples: save samples in addition to sample mean, 5% quantile, SD.
        :param return_samples: return summarised samples in addition to saving them in `self.samples`
        :param mean_field_slot: string, which mean_field slot to sample? 'init_1' by default
        :return: nothing, a dictionary of dictionaries (mean, 5% quantile, SD, optionally all samples) with numpy arrays for each variables is added to self.adata.uns['mod'].
        Optional dictionary of all samples contains parameters as numpy arrays of shape ``(n_samples, ...)``
        """

        self.n_samples = n_samples
        self.n_sampl_batches = int(np.ceil(n_samples / num_samples_batch))
        self.num_samples_batch = num_samples_batch

        if node == "all":
            # Sample all parameters - might use a lot of GPU memory

            self.sample_all(
                self.n_sampl_batches, num_samples_batch=self.num_samples_batch
            )

            self.param_names = list(self.adata.uns["mod"]["post_samples"].keys())

            self.adata.uns["mod"]["post_sample_means"] = {
                v: self.adata.uns["mod"]["post_samples"][v].mean(axis=0)
                for v in self.param_names
            }
            self.adata.uns["mod"]["post_sample_q05"] = {
                v: np.quantile(self.adata.uns["mod"]["post_samples"][v], 0.05, axis=0)
                for v in self.param_names
            }
            self.adata.uns["mod"]["post_sample_q95"] = {
                v: np.quantile(self.adata.uns["mod"]["post_samples"][v], 0.95, axis=0)
                for v in self.param_names
            }
            self.adata.uns["mod"]["post_sample_sds"] = {
                v: self.adata.uns["mod"]["post_samples"][v].std(axis=0)
                for v in self.param_names
            }

            if not save_samples:
                del self.adata.uns["mod"]["post_samples"]

        else:
            self.sample_node(
                node, self.n_sampl_batches, batch_size=self.num_samples_batch, suff=""
            )


class PltExportMixin:
    def plot_posterior_mu_vs_data(self, mu_node_name="mu", data_node="X_data"):
        r"""Plot expected value of the model (e.g. mean of poisson distribution)

        :param mu_node_name: name of the object slot containing expected value
        :param data_node: name of the object slot containing data
        """

        if type(mu_node_name) is str:
            mu = getattr(self, mu_node_name)
        else:
            mu = mu_node_name

        if type(data_node) is str:
            data_node = getattr(self, data_node)

        plt.hist2d(
            np.log10(data_node.flatten() + 1),
            np.log10(mu.flatten() + 1),
            bins=50,
            norm=matplotlib.colors.LogNorm(),
        )
        plt.gca().set_aspect("equal", adjustable="box")
        plt.xlabel("Data, log10(nUMI)")
        plt.ylabel("Posterior sample, log10(nUMI)")
        plt.title("UMI counts (all cell, all genes)")
        plt.tight_layout()

    def plot_history(
        self, iter_start=0, iter_end=-1, history_key=None, log_y=True, ax=None
    ):
        r"""Plot training history

        :param iter_start: omit initial iterations from the plot
        :param iter_end: omit last iterations from the plot
        """

        if ax is None:
            ax = plt
            ax.set_xlabel = plt.xlabel
            ax.set_ylabel = plt.ylabel

        if history_key is None:
            history_key = self.history.keys()

        if type(history_key) == str:
            history_key = [history_key]

        for i in history_key:

            if iter_end == -1:
                iter_end = np.array(self.history[i]).flatten().shape[0]

            y = np.array(self.history[i]).flatten()[iter_start:iter_end]
            if log_y:
                y = np.log10(y)
            ax.plot(np.arange(iter_start, iter_end), y, label="train")
            ax.set_xlabel("Training epochs")
            ax.set_ylabel("Reconstruction accuracy (-ELBO loss)")
            ax.legend()
            plt.tight_layout()

    def export2adata(self, adata, slot_name="mod"):
        r"""Add posterior mean and sd for all parameters to unstructured data `adata.uns['mod']`.

        :param adata: anndata object
        """
        # add factor filter and samples of all parameters to unstructured data
        adata.uns[slot_name] = {}

        adata.uns[slot_name]["mod_name"] = str(self.module.__class__.__name__)
        adata.uns[slot_name]["fact_filt"] = self.fact_filt
        adata.uns[slot_name]["fact_names"] = self.fact_names.tolist()
        adata.uns[slot_name]["var_names"] = self.var_names.tolist()
        adata.uns[slot_name]["obs_names"] = self.obs_names.tolist()
        adata.uns[slot_name]["post_sample_means"] = self.samples["post_sample_means"]
        adata.uns[slot_name]["post_sample_sds"] = self.samples["post_sample_sds"]
        adata.uns[slot_name]["post_sample_q05"] = self.samples["post_sample_q05"]
        adata.uns[slot_name]["post_sample_q95"] = self.samples["post_sample_q95"]

        return adata