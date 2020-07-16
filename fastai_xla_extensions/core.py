# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/00_core.ipynb (unless otherwise specified).

__all__ = ['XLA_AVAILABLE', 'XLAOptimProxy', 'XLAOptFuncWrapper']

# Cell
#colab
XLA_AVAILABLE = True
try:
    import torch_xla.core.xla_model as xm
except ImportError as e:
    XLA_AVAILABLE = False
    import warnings
    # warnings.warn('fastai_xla_extensions requires Pytorch-XLA, will revert to default',
    #              RuntimeWarning)

# Cell
if not XLA_AVAILABLE:
    from types import SimpleNamespace
    import torch.cuda
    def fake_opt_step(opt,barrier=False):
        opt.step()
    def fake_device():
        gpu_available = torch.cuda.is_available()
        return torch.device(torch.cuda.current_device()) if gpu_available else torch.device('cpu')
    xm = SimpleNamespace(
        optimizer_step = fake_opt_step,
        xla_device = fake_device
    )


# Cell
class XLAOptimProxy:
    "Proxy optimizer to override `opt.step` with Pytorch XLA sync method `xm.optimizer_step` "
    def __init__(self,opt):
        self.opt = opt

    def xla_step(self):
        xm.optimizer_step(self.opt,barrier=True) # sync on gradient update

    def __getattr__(self,name):
        if name == 'step': # override proxying for step method
                return getattr(self,'xla_step')
        # proxy everything else
        return getattr(self.opt,name)

# Cell
class XLAOptFuncWrapper:
    "Wrap opt_func to create an optimizer proxied by `XLAOptimProxy`"
    def __init__(self, f):
        self.f = f
    def __call__(self, *args, **kwargs):
        opt = self.f(*args, **kwargs)
        optim_proxy = XLAOptimProxy(opt)
        return optim_proxy


# Cell
if XLA_AVAILABLE:
    from fastcore.foundation import defaults
    defaults.tpu_device = xm.xla_device(devkind='TPU')
    defaults.tpu_available = defaults.tpu_device != None

# Cell
if XLA_AVAILABLE and defaults.tpu_available:
    import fastai2.torch_core
    from fastai2.torch_core import apply
    from torch import Tensor
    def default_device(use_cuda=-1):
        "Return `TPU` as default device"
        return defaults.tpu_device
    def to_device(b, device=None):
        "Recursively put `b` on `device`."
        if device is None: device=default_device()
        # print(f'setting device to {device}')
        def _inner(o): return o.to(device, non_blocking=True) if isinstance(o,Tensor) else o.to_device(device) if hasattr(o, "to_device") else o
        return apply(_inner, b)

    fastai2.torch_core.default_device = default_device
    fastai2.torch_core.to_device = to_device

# Cell
if XLA_AVAILABLE and defaults.tpu_available:
    from fastai2.learner import Learner
    from fastcore.foundation import patch_to

    orig_create_opt = Learner.create_opt
    @patch_to(Learner)
    def create_opt(self):
        orig_opt_func = self.opt_func
        self.opt_func = XLAOptFuncWrapper(orig_opt_func)
        orig_create_opt(self)
