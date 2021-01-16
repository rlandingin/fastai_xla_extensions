# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/03_multi_core.ipynb (unless otherwise specified).

__all__ = ['__getstate__', '__setstate__', 'TPUDistributedDL', 'make_distributed_dataloaders', 'wrap_parallel_loader',
           'XLATrainingCallback', 'build_dataloaders', 'ExtendedModel', 'xla_cnn_model', 'xla_cnn_learner']

# Cell
from fastcore.basics import patch_to
from fastai.optimizer import _BaseOptimizer

@patch_to(_BaseOptimizer)
def __getstate__(self):
    d = {
            'state': self.state_dict(),
            'param_groups': self.param_groups,
        }
    if hasattr(self,'defaults'):
        d['defaults'] = self.defaults
    return d

@patch_to(_BaseOptimizer)
def __setstate__(self, data):
    if 'defaults' in data:
        self.defaults = data['defaults']
    self.load_state_dict(data['state'])
    self.param_groups = data['param_groups']

# Internal Cell
import torch_xla
import torch_xla.core.xla_model as xm
import torch_xla.distributed.parallel_loader as pl
from fastai.data.core import DataLoaders

import math
from fastcore.basics import store_attr
from operator import attrgetter
from fastai.data.load import _FakeLoader
from fastai.data.core import TfmdDL
from fastai.torch_core import TensorBase
import random
import torch

# Cell
def _recast2tensor(o):
    if isinstance(o,TensorBase):
        # return plain tensor since pl.parallelloader doesn't
        # seem to work with tensor subclasses
        return torch.from_numpy(o.numpy()) # should not copy tensor
    return o

def _round_to_multiple(number,multiple):
    return int(math.ceil(number/multiple)*multiple)

class TPUDistributedDL(TfmdDL):
    "A `TfmdDL` which splits a batch into equal size pieces for each TPU core"
    _default = 'dl'
    def __init__(self,dl,rank,world_size, seed=42):
        store_attr()
        self.bs,self.device,self.num_workers,self.drop_last,self.dataset,self.offs,fake = \
            attrgetter('bs','device','num_workers','drop_last','dataset','offs','fake_l')(dl)
        self.fake_l = _FakeLoader(self, fake.pin_memory, fake.num_workers, fake.timeout,
                                  persistent_workers=fake.persistent_workers)
        self.epoch = 0
        random.seed(self.seed)
        self.dl.rng = random.Random(random.randint(0,2**32-1))
        self.reset_rng()

    def reset_rng(self):
        random.seed(self.seed + self.epoch)
        self.rng = random.Random(random.randint(0,2**32-1))

    def __len__(self):
        return _round_to_multiple(len(self.dl),self.world_size)//self.world_size

    def set_epoch(self, epoch):
        self.epoch = epoch

    def get_idxs(self):
        idxs = self.dl.get_idxs()
        # do your own shuffling which factors in self.epoch + self.seed in
        # generating a random sequence (underlying self.dl does not)
        if self.shuffle:
            idxs = self.shuffle_fn(idxs)
        self.n = len(idxs)
        # we assumed n was dl.n but we really care about number of idxs
        # add extra samples to make it evenly divisible
        self.n_padded = _round_to_multiple(self.n,self.world_size)
        idxs += (idxs * (self.n_padded//self.n))[:self.n_padded-self.n]
        # idx needs to be repeated when n_padded>>n
        # slice padded idxs so that each rank gets self.n_padded//self.world_size tensors
        start_pos = self.rank*self.n_padded//self.world_size
        end_pos = (self.rank+1)*self.n_padded//self.world_size
        return idxs[start_pos:end_pos]

    def before_iter(self):
        self.dl.before_iter()

    def randomize(self):
        self.reset_rng()
        self.dl.randomize()

    def after_batch(self,b):
        b = self.dl.after_batch(b)
        # recast tensor subclasses to plain tensors
        # undoing work of self.retain()
        tb = [_recast2tensor(o) for o in b]
        b = tuple(tb)
        return b

    def after_iter(self):
        self.dl.after_iter()

    def create_batches(self,samps):
        return self.dl.create_batches(samps)

    def to(self, device):
        self.dl.device = device
        self.device = device
        return self

# Cell
def make_distributed_dataloaders(dls, rank, world_size):
    new_loaders = []
    for i,dl in enumerate(dls.loaders):
        if i == 0:
            use_rank = rank
            use_size = world_size
        else:
            # for now, in validation, use all samples since only rank 0 computes
            # the valid loss and metrics
            # TODO: figure out a way to consolidate valid loss and metrics across
            # ranks to make distribute batches across multi cores (and reduce number
            # of batches per rank -- which should speed up validation)
            use_rank = 0
            use_size = 1
        dl = TPUDistributedDL(dl,
                            rank=use_rank,
                            world_size=use_size)
        new_loaders += [dl]
    return DataLoaders(*new_loaders, path=dls.path, device=dls.device)

# Internal Cell
import torch.utils.hooks
from fastcore.basics import patch

# Cell
def wrap_parallel_loader(loader, device):
    para_loader = pl.ParallelLoader(loader, [device])
    loop_loader = para_loader.per_device_loader(device)
    return loop_loader

# Internal Cell
from fastai.callback.core import TrainEvalCallback
from fastai.learner import Recorder
import torch
from fastai.callback.core import Callback
from fastai.learner import CancelValidException, CancelStepException
from fastai.torch_core import tensor

# Cell
class XLATrainingCallback(Callback):
    run_before = Recorder
    run_valid = False
    order = -10 # same as TrainEvalCallback (since this replaces TrainEvalCallback)
    def __init__(self, device, rank=0):
        self.pdevice = device
        self.rank = rank

    def after_create(self):
        self.learn.n_epoch = 1

    def before_fit(self):
        "Set the iter and epoch counters to 0, put the model and the right device"
        self.learn.epoch,self.learn.loss = 0,tensor(0.)
        self.learn.train_iter,self.learn.pct_train = 0,0.
        if hasattr(self.dls, 'device'): self.model.to(self.dls.device)
        if hasattr(self.model, 'reset'): self.model.reset()
        xm.master_print(' ')

    def before_epoch(self):
        # set the epoch on train only to make sure shuffle produces same seq
        # across all ranks
        if hasattr(self.learn.dls.train,'sampler'):
            if hasattr(self.learn.dls.train.sampler,'set_epoch'):
                self.learn.dls.train.sampler.set_epoch(self.learn.epoch)
        elif hasattr(self.learn.dls.train,'set_epoch'):
            self.learn.dls.train.set_epoch(self.learn.epoch)

    def before_train(self):
        "Set the model in training mode"
        self.learn.pct_train=self.epoch/self.n_epoch
        self.model.train()
        self.learn.training=True
        self.learn.dl = wrap_parallel_loader(self.dls.train, self.pdevice)

    def before_validate(self):
        "Set the model in validation mode"
        if self.rank != 0: # no need to compute valid loss/ metric if not master
            raise CancelValidException()
        self.model.eval()
        self.learn.training=False
        self.learn.dl = wrap_parallel_loader(self.dls.valid, self.pdevice)

    def before_step(self):
        raise CancelStepException()

    def after_cancel_step(self):
        xm.optimizer_step(self.learn.opt)

    def after_batch(self):
        "Update the iter counter (in training mode)"
        self.learn.pct_train += 1./(self.n_iter*self.n_epoch)
        self.learn.train_iter += 1

# Internal Cell
from fastai.learner import Learner
from fastai.callback.progress import ProgressCallback
from fastcore.xtras import join_path_file

# Cell

@patch
def save(self:Learner, file, **kwargs):
    file = join_path_file(file, self.path/self.model_dir, ext='.pth')
    with_opt = self.opt is not None
    state = self.model.state_dict()
    if with_opt:
        opt_state = self.opt.state_dict()
        state = {'model': state, 'opt':opt_state}
    xm.save(state, file) # use xm.save instead of torch.save
    return file

# Cell

@patch
def to_xla(self:Learner,device, rank):
    if 'xla_training' not in self.cbs.attrgot('name'):
        self.dls.device = None
        self.add_cbs(XLATrainingCallback(device, rank))
    else:
        self.xla_training.pdevice = device
        self.xla_training.rank = rank

    self.remove_cbs(TrainEvalCallback) # replace TrainEval with XLATraining

    if rank != 0:
        self.remove_cbs(ProgressCallback)
    self.logger = xm.master_print

# Cell

# def DataBlock.dataloaders(self, source, path='.', verbose=False, **kwargs):
def build_dataloaders(datablock, source, rank, world_size, device=None, path='.', verbose=False,**kwargs):
    dls = datablock.dataloaders(source=source, path=path, device=device, **kwargs)
    distrib_dls = make_distributed_dataloaders(dls, rank, world_size)
    return distrib_dls

# Internal Cell
#from fastcore.basics import store_attr

# Cell
class ExtendedModel:
    def __init__(self, arch, normalize, n_out, pretrained):
        # store_attr()
        self.arch = arch
        self.normalize = normalize
        self.n_out = n_out
        self.pretrained = pretrained

# Internal Cell
from fastai.vision.learner import create_cnn_model

# Cell
def xla_cnn_model(arch,
                  n_out,
                  normalize=True,
                  pretrained=True,
                **kwargs):
    "Build a convnet style learner from `dls` and `arch`"
    assert n_out, "`n_out` is not defined, and could not be inferred from data, set `dls.c` or pass `n_out`"
    # set concat_pool to false because AdaptiveConcatPool not supported in XLA
    if 'concat_pool' in kwargs:
        kwargs.pop('concat_pool',None)
    model = create_cnn_model(arch, n_out, pretrained=pretrained, concat_pool=False, **kwargs)
    ext_model = ExtendedModel(arch, normalize, n_out, pretrained)
    return ext_model, model

# Internal Cell
from fastai.optimizer import Adam
from fastai.learner import defaults
from fastai.vision.learner import model_meta, _add_norm, _default_meta
from fastcore.basics import ifnone

# Cell
def xla_cnn_learner(dls,
                    ext_model,
                    model,
                    loss_func=None,
                    opt_func=Adam,
                    lr=defaults.lr,
                    splitter=None,
                    cbs=None,
                    metrics=None,
                    path=None,
                    model_dir='models',
                    wd=None,
                    wd_bn_bias=False,
                    train_bn=True,
                    moms=(0.95,0.85,0.95),
                    # other model args
                    **kwargs):
    "Build a convnet style learner from `dls` and `ext_model`"

    meta = model_meta.get(ext_model.arch, _default_meta)
    if ext_model.normalize: _add_norm(dls, meta, ext_model.pretrained)

    assert ext_model.n_out is not None, "`n_out` is not defined please pass `n_out`"
    # device = dls.device if hasattr(dls,'device') and dls.device is not None else xm.xla_device()
    # device = xm.xla_device()
    # model = ext_model.model.to(device) # xmp wrapped model
    splitter=ifnone(splitter, meta['split'])
    learn = Learner(dls=dls, model=model, loss_func=loss_func, opt_func=opt_func, lr=lr, splitter=splitter, cbs=cbs,
                   metrics=metrics, path=path, model_dir=model_dir, wd=wd, wd_bn_bias=wd_bn_bias, train_bn=train_bn,
                   moms=moms)
    if ext_model.pretrained: learn.freeze()
    # keep track of args for loggers
    # store_attr('arch,normalize,n_out,pretrained', self=learn, **kwargs)
    learn.arch = ext_model.arch
    learn.normalize = ext_model.normalize
    learn.n_out = ext_model.n_out
    learn.pretrained = ext_model.pretrained
    return learn