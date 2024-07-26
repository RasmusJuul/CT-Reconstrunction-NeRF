import torch
import math
import numpy as np
from pytorch_lightning import LightningModule
import torchmetrics as tm
import tifffile
from tqdm import tqdm
# import tinycudann as tcnn
from src.encoder import get_encoder

def get_activation_function(activation_function,args_dict,**kwargs):
    if activation_function == 'relu':
        return torch.nn.ReLU(**kwargs)
    elif activation_function == 'leaky_relu':
        return torch.nn.LeakyReLU(**kwargs)
    elif activation_function == 'sigmoid':
        return torch.nn.Sigmoid(**kwargs)
    elif activation_function == 'tanh':
        return torch.nn.Tanh(**kwargs)
    elif activation_function == 'elu':
        return torch.nn.ELU(**kwargs)
    elif activation_function == 'none':
        return torch.nn.Identity(**kwargs)
    elif activation_function == 'sine':
        return torch.jit.script(Sine(**kwargs)).to(device=args_dict['training']['device'])
    else:
        raise ValueError(f"Unknown activation function: {activation_function}")
        
class Sine(torch.nn.Module):
    def __init(self):
        super().__init__()

    def forward(self, input: torch.Tensor
               ) -> torch.Tensor:
        # See Siren paper sec. 3.2, final paragraph, and supplement Sec. 1.5 for discussion of factor 30
        return torch.sin(30 * input)


def sine_init(m):
    with torch.no_grad():
        if hasattr(m, 'weight'):
            num_input = m.weight.size(-1)
            # In siren paper see supplement Sec. 1.5 for discussion of factor 30
            m.weight.uniform_(-np.sqrt(6 / num_input) / 30, np.sqrt(6 / num_input) / 30)


def first_layer_sine_init(m):
    with torch.no_grad():
        if hasattr(m, 'weight'):
            num_input = m.weight.size(-1)
            m.weight.uniform_(-1 / num_input, 1 / num_input)

@torch.jit.script
def compute_projection_values(num_points: int,
                              attenuation_values: torch.Tensor,
                              lengths: torch.Tensor,
                             ) -> torch.Tensor:
    I0 = 1
    # Compute the spacing between ray points
    dx = lengths / (num_points)

    # Compute the sum of mu * dx along each ray
    attenuation_sum = torch.sum(attenuation_values * dx[:,None], dim=1)

    return attenuation_sum

def lr_lambda(epoch: int):
    if epoch <= 50:
        return 1.0
    else:
        return 0.97 ** (epoch-50)

class MLP(LightningModule):

    def __init__(self,args_dict,projection_shape,num_volumes):
        super(MLP,self).__init__()
        self.save_hyperparameters()

        self.projection_shape = projection_shape
        self.lr = args_dict['training']['learning_rate']
        self.imagefit_mode = args_dict["training"]["imagefit_mode"]

        self.l1_regularization_weight = args_dict['training']['regularization_weight']

        self.num_freq_bands = args_dict['model']['num_freq_bands']
        self.num_hidden_layers = args_dict['model']['num_hidden_layers']
        self.num_hidden_features = args_dict['model']['num_hidden_features']
        self.activation_function = args_dict['model']['activation_function']
        self.latent_size = args_dict['model']['latent_size']

        # Initialising encoder
        if args_dict['model']['encoder'] != None:
            self.encoder = get_encoder(encoding=args_dict['model']['encoder'])
            num_input_features = self.encoder.output_dim
        else:
            self.encoder = None
            num_input_features = 3 # x,y,z coordinate

        if self.imagefit_mode:
            # Initialising latent vectors
            self.lat_vecs = torch.nn.Embedding(num_volumes,self.latent_size)
            torch.nn.init.normal_(self.lat_vecs.weight.data,
                                  0.0,
                                  1 / math.sqrt(self.latent_size),
                                )
            num_input_features += self.latent_size

        layers = []
        for i in range(self.num_hidden_layers):
            layers.append(torch.nn.Sequential(torch.nn.Linear(self.num_hidden_features,self.num_hidden_features),
                                         get_activation_function(self.activation_function,args_dict),
                                         ))


        self.mlp = torch.nn.Sequential(torch.nn.Linear(num_input_features,self.num_hidden_features),
                                       get_activation_function(self.activation_function,args_dict),
                                       *layers,
                                        torch.nn.Linear(self.num_hidden_features,1),
                                        torch.nn.Sigmoid(),
                                        )

        if self.activation_function == 'sine':
            self.mlp.apply(sine_init)
            self.mlp[0].apply(first_layer_sine_init)

        self.loss_fn = torch.nn.MSELoss()
        self.l1_regularization = torch.nn.L1Loss()
        self.psnr = tm.image.PeakSignalNoiseRatio()
        self.validation_step_outputs = []
        self.validation_step_gt = []


    def forward(self, pts, vecs):
        pts_shape = pts.shape

        if len(pts.shape) > 2:
            pts = pts.view(-1,3)

        if self.encoder != None:
            enc = self.encoder(pts)
        else:
            enc = pts
            
        enc = torch.cat([vecs, enc], dim=1)

        out = self.mlp(enc)

        if len(pts_shape) > 2:
            out = out.view(*pts_shape[:-1],-1)

        return out

    def training_step(self, batch, batch_idx):
        # training_step defines the train loop.
        points, target, idxs = batch
        if self.imagefit_mode:
            attenuation_values = self.forward(points,self.lat_vecs(idxs).repeat(1,points.shape[1],points.shape[2],1).view(-1,self.latent_size))
            attenuation_values = attenuation_values.view(target.shape)
                
            loss = self.loss_fn(attenuation_values,target)
            
            self.log_dict(
                {
                    "train/loss": loss,
                },
                on_step=True,
                on_epoch=True,
                sync_dist=True,
            )
            
            return loss
        else:
            lengths = torch.linalg.norm((points[:,-1,:] - points[:,0,:]),dim=1)
            attenuation_values = self.forward(points, idxs).view(points.shape[0],points.shape[1])
            detector_value_hat = compute_projection_values(points.shape[1],attenuation_values,lengths)

            smoothness_loss = self.l1_regularization(attenuation_values[:,1:],attenuation_values[:,:-1]) # punish model for big changes between adjacent points (to make it smooth)
            loss = self.loss_fn(detector_value_hat, target)

            total_loss = loss + self.l1_regularization_weight*smoothness_loss
        
            self.log_dict(
                {
                    "train/loss": loss,
                    "train/loss_total":total_loss,
                    "train/l1_regularization":smoothness_loss,
                },
                on_step=True,
                on_epoch=True,
                sync_dist=True,
            )
            
            return total_loss

    def validation_step(self, batch, batch_idx):
        points, target, idxs = batch
        if self.imagefit_mode:
            attenuation_values = self.forward(points,self.lat_vecs(idxs).repeat(1,points.shape[1],points.shape[2],1).view(-1,self.latent_size))
            attenuation_values = attenuation_values.view(target.shape)
            
            loss = self.loss_fn(attenuation_values,target)
            
            self.validation_step_outputs.append(attenuation_values)
            self.validation_step_gt.append(target)
        else:
            lengths = torch.linalg.norm((points[:,-1,:] - points[:,0,:]),dim=1)
            attenuation_values = self.forward(points, idxs).view(points.shape[0],points.shape[1])
            detector_value_hat = compute_projection_values(points.shape[1],attenuation_values,lengths)

            smoothness_loss = self.l1_regularization(attenuation_values[:,1:],attenuation_values[:,:-1])
    
            loss = self.loss_fn(detector_value_hat, target)
            
            self.validation_step_outputs.append(detector_value_hat.detach().cpu())
            self.validation_step_gt.append(target.detach().cpu())

            total_loss = loss + self.l1_regularization_weight*smoothness_loss

        self.log_dict(
            {
                "val/loss": loss,
                "val/loss_total":total_loss,
                "val/l1_regularization":smoothness_loss,
            },
            on_step=False,
            on_epoch=True,
            sync_dist=True,
        )
        

    def on_validation_epoch_end(self):     
        all_preds = torch.cat(self.validation_step_outputs)
        all_gt = torch.cat(self.validation_step_gt)
        vol = self.trainer.val_dataloaders.dataset.vol.to(device=self.device)
        self.vol_shape = vol.shape
        if self.imagefit_mode:
            preds = all_preds.view(self.vol_shape)
            gt = all_gt.view(self.vol_shape)
            self.logger.log_image(key="val/reconstruction", images=[preds[self.vol_shape[2]//2,:,:], gt[self.vol_shape[2]//2,:,:]], caption=["pred", "gt"]) # log projection images
            psnr = self.psnr(preds.unsqueeze(dim=0).unsqueeze(dim=0),gt.unsqueeze(dim=0).unsqueeze(dim=0))
            self.log("val/reconstruction",psnr)
        else:
            valid_rays = self.trainer.val_dataloaders.dataset.valid_rays.view(self.projection_shape)
            preds = torch.zeros(self.projection_shape,dtype=all_preds.dtype)
            preds[valid_rays] = all_preds
            gt = torch.zeros(self.projection_shape,dtype=all_gt.dtype)
            gt[valid_rays] = all_gt

            for i in np.random.randint(0,self.projection_shape[0],5):
                self.logger.log_image(key="val/projection", images=[preds[i], gt[i], (gt[i]-preds[i])], caption=[f"pred_{i}", f"gt_{i}",f"residual_{i}"]) # log projection images

            vol = self.trainer.val_dataloaders.dataset.vol
            mgrid = torch.stack(torch.meshgrid(torch.linspace(-1, 1, vol.shape[0]), torch.linspace(-1, 1, vol.shape[0]), torch.linspace(-1, 1, vol.shape[0]), indexing='ij'),dim=-1)
            mgrid = mgrid.view(-1,vol.shape[2],3)
            outputs = torch.zeros((*mgrid.shape[:2],1))
            with torch.no_grad():
                for i in range(mgrid.shape[1]):
                    output = self.forward(mgrid[:,i,:].to(device=self.device))
                    
                    outputs[:,i,:] = output.cpu()
    
                outputs = outputs.view(self.vol_shape)

            self.log("val/loss_reconstruction",self.loss_fn(outputs,vol))
            self.logger.log_image(key="val/reconstruction", images=[outputs[self.vol_shape[2]//2,:,:], vol[self.vol_shape[2]//2,:,:], (vol[self.vol_shape[2]//2,:,:]-outputs[self.vol_shape[2]//2,:,:]),
                                                                    outputs[:,self.vol_shape[2]//2,:], vol[:,self.vol_shape[2]//2,:], (vol[:,self.vol_shape[2]//2,:]-outputs[:,self.vol_shape[2]//2,:]),
                                                                    outputs[:,:,self.vol_shape[2]//2], vol[:,:,self.vol_shape[2]//2], (vol[:,:,self.vol_shape[2]//2]-outputs[:,:,self.vol_shape[2]//2])],
                                  caption=["pred_xy", "gt_xy","residual_xy",
                                           "pred_yz", "gt_yz","residual_yz",
                                           "pred_xz", "gt_xz","residual_xz",])
            

        self.validation_step_outputs.clear()  # free memory
        self.validation_step_gt.clear()  # free memory

    def test_step(self, batch, batch_idx):
        return None

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.lr, amsgrad=True)
        # return optimizer  
        # lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lr_lambda)
        if self.imagefit_mode:
            lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, 10, T_mult=2)
        else:
            lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, 50, T_mult=2)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "epoch",}
        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler_config}
        



