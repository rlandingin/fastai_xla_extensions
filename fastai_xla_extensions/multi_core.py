# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/03_multi_core.ipynb (unless otherwise specified).

__all__ = ['__getstate__', '__setstate__', 'TPUDistributedDL', 'TfmdTorchDS', 'to_list', 'has_setup', 'run_setups',
           'TorchDatasetBuilder', 'VocabularyMapper', 'to', 'make_torch_dataloaders', 'make_distributed_dataloaders',
           'wrap_parallel_loader', 'XLATrainingCallback', 'compute_batch_mean_loss', 'unpack_sync', 'LossTrackerMetric',
           'SyncRecorderCallback', 'build_dataloaders', 'ExtendedModel', 'xla_cnn_model', 'xla_cnn_learner']

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

# Internal Cell
from fastcore.basics import patch_to
import torch.utils.data.distributed as th_distrib
import torch.utils.data as th_data

# Cell
class TfmdTorchDS(th_data.Dataset):
    def __init__(self, items, x_tfm=None, y_tfm=None):
        self.items = items
        self.x_tfm = x_tfm
        self.y_tfm = y_tfm

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        item = self.items[index]
        x = self.x_tfm(item) if self.x_tfm is not None else x
        y = self.y_tfm(item) if self.y_tfm is not None else y
        return (x,y)

# Cell
from fastcore.xtras import is_listy
import torchvision as thv
from operator import itemgetter
from fastcore.imports import noop
from fastcore.foundation import L

def to_list(o):
    return [] if o is None else [o] if not is_listy(o) else o

def has_setup(tfms):
    """returns last index if at least 1 `tfm` in `tfms` has a method `setup` else return -1"""
    setups = L(tfms).attrgot('setup',None).argwhere(noop) # get indexes where tfm has `setup` attribute
    return -1 if len(setups) == 0 else setups[-1]

def run_setups(tfms, items):
    """run tfm setups including tfm for all items"""
    indx = has_setup(tfms)
    if indx == -1: # no setup found
        return

    for i,tfm in enumerate(tfms):
        if hasattr(tfm,'setup'):
            tfm.setup(items)
        if i < indx:
            # tfm items to be fed into next tfm
            items = [tfm(item) for item in items]


class TorchDatasetBuilder:
    def __init__(self, source, get_items, splitter,
                x_tfms, y_tfms,
                x_type_tfms=None,
                x_train_tfms=None, x_test_tfms=None,
                do_setup=False):
        self.source = source
        self.get_items = get_items
        self.splitter = splitter
        self.do_setup = do_setup
        self.x_tfms = to_list(x_tfms)
        self.y_tfms = to_list(y_tfms)
        self.x_type_tfms = to_list(x_type_tfms)
        self.x_train_tfms = to_list(x_train_tfms)
        self.x_test_tfms = to_list(x_test_tfms)

    def setup(self, items, do_setup=None, setup_x=False):
        self.do_setup = do_setup if do_setup is not None else self.do_setup
        if self.do_setup:
            all_x_tfms = [*self.x_type_tfms, *self.x_train_tfms, *self.x_tfms]
            if setup_x:
                run_setups(all_x_tfms, items)
            run_setups(self.y_tfms, items)
            self.do_setup = False

    def get_datasets(self, do_setup=None):
        self.do_setup = do_setup if do_setup is not None else self.do_setup
        items = self.get_items(self.source)
        train_idxs, test_idxs = self.splitter(items)

        train_items = itemgetter(*train_idxs)(items)
        test_items = itemgetter(*test_idxs)(items)
        self.setup(train_items)
        allx_test_tfms = [*self.x_type_tfms, *self.x_test_tfms, *self.x_tfms]
        allx_train_tfms = [*self.x_type_tfms, *self.x_train_tfms, *self.x_tfms]
        train_x_tfm = thv.transforms.Compose(allx_train_tfms)
        test_x_tfm = thv.transforms.Compose(allx_test_tfms)
        y_tfm = thv.transforms.Compose(self.y_tfms)
        train_ds = TfmdTorchDS(train_items, x_tfm=train_x_tfm, y_tfm=y_tfm)
        test_ds = TfmdTorchDS(test_items, x_tfm=test_x_tfm, y_tfm=y_tfm)
        return train_ds, test_ds

# Cell
from fastai.data.transforms import CategoryMap

class VocabularyMapper:
    """A simplified version of the fastai Categorize Transform"""
    def __init__(self, vocab=None):
        self.vocab = vocab
        self.c = 0
    def setup(self, items):
        self.vocab = CategoryMap(items)
        self.c = len(self.vocab)
    def __call__(self, o):
        if self.vocab is None: return o
        try:
            return torch.tensor(self.vocab.o2i[o])
        except KeyError as e:
            raise KeyError(f"Label '{o}' was not included in the training dataset") from e

# Cell
@patch_to(th_data.DataLoader)
def to(self, device):
    self.device = device

# Cell
def make_torch_dataloaders(train_dataset, test_dataset,
                     rank,
                     world_size,
                     bs,
                     num_workers=4,
                     distrib=True):
    if distrib:
        train_sampler = th_distrib.DistributedSampler(
            train_dataset,
            num_replicas=world_size,
            rank=rank,
            shuffle=True)
        train_loader = th_data.DataLoader(
            train_dataset,
            batch_size=bs,
            sampler=train_sampler,
            # shuffle=True,
            num_workers=num_workers,
            drop_last=True)
        test_sampler = th_distrib.DistributedSampler(
            train_dataset,
            num_replicas=world_size,
            rank=rank,
            shuffle=False)

        test_loader = th_data.DataLoader(
            test_dataset,
            batch_size=bs,
            sampler=test_sampler,
            # shuffle=False,
            num_workers=num_workers,
            drop_last=True)

    else:
        train_loader = th_data.DataLoader(
            train_dataset,
            batch_size=bs,
            # sampler=train_sampler,
            shuffle=True,
            num_workers=num_workers,
            drop_last=True)

        test_loader = th_data.DataLoader(
            test_dataset,
            batch_size=bs,
            shuffle=False,
            num_workers=num_workers,
            drop_last=True)
    dataloaders = DataLoaders(train_loader, test_loader, device=None)
    return dataloaders

# Cell
def make_distributed_dataloaders(dls, rank, world_size):
    new_loaders = [TPUDistributedDL(dl, rank=rank, world_size=world_size) for dl in dls.loaders]
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

        # set the epoch on train and test to make sure shuffle produces same seq
        if hasattr(self.learn.dls.train,'sampler'):
            if hasattr(self.learn.dls.train.sampler,'set_epoch'):
                self.learn.dls.train.sampler.set_epoch(self.learn.epoch)
        elif hasattr(self.learn.dls.train,'set_epoch'):
            self.learn.dls.train.set_epoch(self.learn.epoch)

        if hasattr(self.learn.dls.valid,'sampler'):
            if hasattr(self.learn.dls.valid.sampler,'set_epoch'):
                self.learn.dls.valid.sampler.set_epoch(self.learn.epoch)
        elif hasattr(self.learn.dls.valid,'set_epoch'):
            self.learn.dls.valid.set_epoch(self.learn.epoch)

    def before_train(self):
        "Set the model in training mode"
        self.learn.pct_train=self.epoch/self.n_epoch
        self.model.train()
        self.learn.training=True
        self.learn.dl = wrap_parallel_loader(self.dls.train, self.pdevice)

    def before_validate(self):
        "Set the model in validation mode"
#         if self.rank != 0: # no need to compute valid loss/ metric if not master
#             raise CancelValidException()
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
import copy
from fastcore.imports import noop
from fastcore.foundation import L
from fastai.learner import Metric, AvgMetric, AvgLoss, AvgSmoothLoss
import torch
import pickle
from fastai.torch_core import find_bs, to_detach

# Cell

@patch
def update_metric(self:Metric, other_metrics, other_losses):
    # dunno how to handle updates for metrics other than AvgMetric, AvgLoss, AvgSmoothLoss
    pass

@patch
def update_metric(self:(AvgMetric,AvgLoss), other_metrics, other_losses):
    other_metrics = L(other_metrics)
    # other metrics must also be AvgMetric
    assert len(other_metrics.map(lambda o: not isinstance(o, (AvgLoss,AvgMetric))).argwhere(noop)) == 0
    # other metrics must have same name
    assert len(other_metrics.attrgot('name').map(lambda o: o != self.name).argwhere(noop)) == 0
    self.total = other_metrics.attrgot('total').sum()
    self.count = other_metrics.attrgot('count').sum()

def compute_batch_mean_loss(i, other_losses):
    batch_losses = other_losses.itemgot(i).attrgot('loss')
    batch_sizes =  other_losses.itemgot(i).attrgot('bs')
    batch_sum_loss = batch_losses.zipwith(batch_sizes).map(lambda xy: xy[0]*xy[1]).sum()
    batch_sum_size = batch_sizes.sum()
    batch_mean_loss = batch_sum_loss / batch_sum_size
    return batch_mean_loss

@patch
def update_metric(self:AvgSmoothLoss, other_metrics, other_losses):
    # reset count,val and beta to start of train (done by SyncRecorderCallback)
    other_losses = L(other_losses)
    n_batches = len(other_losses[0]) # get length of batches from one rank
    smooth_losses = []
    for i in range(n_batches):
        batch_mean_loss = compute_batch_mean_loss(i, other_losses)
        # based on definition of AvgSmoothLoss accumulate, taking into account losses
        # from across all ranks computed as a mean
        self.count += 1
        self.val = torch.lerp(batch_mean_loss, self.val, self.beta)
        smooth_losses.append(self.value)
    self.batch_smooth_losses = smooth_losses

def unpack_sync(res):
    return [pickle.loads(o) for o in res]


# Cell
class LossTrackerMetric(Metric):
    losses = []
    def reset(self):
        self.losses.clear()
    def accumulate(self, learn):
        mean_loss = to_detach(learn.loss.mean(),gather=False)
        bs = find_bs(learn.yb)
        self.losses.append({'loss': mean_loss, 'bs': bs})
    @property
    def value(self):
        return self.losses

# Internal Cell
from fastai.learner import _maybe_item
from fastprogress.fastprogress import format_time
import time

# Cell
class SyncRecorderCallback(Callback):
    """Sync metrics from each spawned process update statistics
       accordingly so it will display correctly in the progress callback
    """
    order  = 55 # after Recorder, before ProgressCallback
    def __init__(self):
        self.loss_tracker = LossTrackerMetric() # only track train loss

    def before_fit(self):
        if not xm.is_master_ordinal():
            return
        if 'progress' in self.learn.cbs.attrgot('name',None):
            self._sync_stats_log = self.progress._write_stats
        else:
            self._sync_stats_log = self.learn.logger

        self.sync_smooth_loss = copy.deepcopy(self.recorder.smooth_loss)
    def after_fit(self):
        xm.rendezvous('sync recorder after_fit')

    def before_epoch(self):
        self.sync_log = copy.copy(self.recorder.log)

    def after_epoch(self):
        if 'recorder' not in self.learn.cbs.attrgot('name'):
            all_metrics = {
                'train_mets': L([]),
                'valid_mets': L([]),
                'losses': L([])
            }
        else:
            all_metrics = {
                'train_mets': self.recorder._train_mets,
                'valid_mets': self.recorder._valid_mets,
                # list of loss,bs for each train batch
                #(for recomputing avg smooth loss)
                'losses': self.loss_tracker.value
            }
        # send metrics data to sync ranks across spawned processes
        sync_tag = f'sync_recorder after epoch{self.learn.epoch}'
        res = xm.rendezvous(sync_tag, pickle.dumps(all_metrics))

        if xm.is_master_ordinal():
            all_metrics = unpack_sync(res)
            self._sync_log(all_metrics) # use metrics across ranks to update log

            if hasattr(self.recorder.smooth_loss,'batch_smooth_losses'):
                # update recorder losses with smooth losses accounting for losses across ranks
                batch_smooth_losses = self.recorder.smooth_loss.batch_smooth_losses
                n_batches = len(self.recorder.losses )
                # delete last set of batches from current epoch
                self.recorder.losses = self.recorder.losses[:n_batches - len(batch_smooth_losses)]
                self.recorder.losses += batch_smooth_losses # replace with batch_smooth_losses

            self.learn.smooth_loss = self.recorder.smooth_loss.value
            self.learn.final_record = self.sync_log[:1].copy()
            del self.recorder.values[-1] # remove last entry added by recorder
            self.recorder.values.append(self.learn.final_record) # add updated metrics
            if self.recorder.add_time:
                updated_time = (time.time() - self.recorder.start_epoch)
                self.sync_log.append(format_time(updated_time))
            self.recorder.log = self.sync_log
            self._sync_stats_log(self.sync_log) # write_stats to output
            del self.recorder.iters[-1] # remove last entry added by recorder
            self.recorder.iters.append(self.recorder.smooth_loss.count) # add updated smooth loss count

            self.learn.logger = self.orig_logger # restore orig logger after skipping recorder.logger(log)

#         self.learn.final_record = self.log[1:].copy()
#         self.values.append(self.learn.final_record)
#         if self.add_time: self.log.append(format_time(time.time() - self.start_epoch))
#         self.logger(self.log)
#         self.iters.append(self.smooth_loss.count)

    def before_train(self):
        self.loss_tracker.reset()
        if xm.is_master_ordinal():
            # find all recorder metrics (count, val, beta) of AvgLossMetric type and store a copy
            self.sync_smooth_loss = copy.deepcopy(self.recorder.smooth_loss)

    def after_train(self):
        if xm.is_master_ordinal():
            # undo all batch updates (count, val, beta) to AvgLossMetric type and reset them to before train.
            self.recorder.smooth_loss.count = self.sync_smooth_loss.count
            self.recorder.smooth_loss.val = self.sync_smooth_loss.val
            self.recorder.smooth_loss.beta = self.sync_smooth_loss.beta

    def before_validate(self):
        pass

    def after_validate(self):
        if xm.is_master_ordinal():
            self.orig_logger = self.learn.logger
            self.learn.logger = noop # write to logger disabled so calling recorder.logger(log) wont print
        pass

    def before_batch(self):
        pass

    def after_batch(self):
        self.loss_tracker.accumulate(self.learn)

    def _sync_log(self, all_metrics):
        all_metrics = L(all_metrics)

        for i,m in enumerate(self.recorder._train_mets):
            m.update_metric(all_metrics.attrgot('train_mets').itemgot(i), all_metrics.attrgot('losses'))
            self.sync_log += _maybe_item(m)

        for i,m in enumerate(self.recorder._valid_mets):
            m.update_metric(all_metrics.attrgot('valid_mets').itemgot(i), all_metrics.attrgot('losses'))
            self.sync_log += _maybe_item(m)


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
        self.add_cbs(SyncRecorderCallback())
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