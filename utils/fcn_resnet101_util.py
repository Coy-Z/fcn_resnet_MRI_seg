import numpy as np
import matplotlib.pyplot as plt
import os
import torch
import torch.nn as nn
import torchvision
from torchvision.transforms import v2 as T
from torch.utils.data import Dataset
from typing import Sequence

class MRIDataset(Dataset):
    '''
    A custom dataset class for processing .npy 3D MRI density scans.
    '''
    def __init__(self, root : str, phase : str = 'val', transform = None, target_transform = None, augment = None):
        '''
        Initialise the MRIDataset daughter class of torch.utils.data.Dataset.

        Args:
            root (str): The string directory of the data.
            phase (str): Indicating whether it is a training or validation dataset.
            transform: The deterministic component of transform (i.e. no random flipping -> validation transform).
            augment: The stochastic component of transform (e.g. random horizontal flip -> additional training transform).
            
        N.B. transform and augment are separate because we must merge the scan and mask to ensure consistent augmentation.
        '''
        super().__init__()
        self.scans = list(sorted(os.listdir(os.path.join(root, "magn")))) # (N * D * H * W)
        self.masks = list(sorted(os.listdir(os.path.join(root, "mask")))) # (N * D * H * W)
        self.root = root
        self.phase = phase
        self.transform = transform
        self.target_transform = target_transform
        self.augment = augment
    
    def __len__(self):
        '''
        Return the number of scans in the Dataset.
        '''
        return len(self.scans)
    
    def __getitem__(self, idx : int) -> tuple[torch.Tensor, torch.Tensor]:
        '''
        Preprocess and return the idx-th scan from the array of scans.

        Args:
            idx (int): The index of the scan we wish to retrieve from the dataset.
        
        Returns:
            scan (torch.Tensor): The pre-processed RGB scan Float32 tensor (D * 3 * H * W).
            mask3d (torch.Tensor): The pre-processed binary mask Int64 tensor (D * H * W).
        '''
        scan_path = os.path.join(self.root, "magn", self.scans[idx])
        mask_path = os.path.join(self.root, "mask", self.masks[idx])
        
        # Load data
        scan = grayscale_to_rgb(np.load(scan_path)) # (D * H * W * 3)
        mask3d_raw = np.load(mask_path) # (D * H * W)
        
        if mask3d_raw.dtype == bool:
            mask3d_raw = mask3d_raw.astype(np.int64)  # False->0, True->1
        
        mask3d = mask3d_raw[..., np.newaxis] # (D * H * W * 1)

        # Apply transforms
        if self.transform:
            scan = torch.stack([self.transform(slice) for slice in scan]) # (D * 3 * H * W)
        if self.target_transform:
            mask3d = torch.stack([self.target_transform(mask) for mask in mask3d]) # (D * 1 * H * W)
        if self.augment and self.phase == 'train':
            data = torch.cat([scan, mask3d], 1) # (D * 4 * H * W)
            data = self.augment(data)

            scan = data[:, :3, :, :] # (D * 3 * H * W)
            mask3d = data[:, 3:, :, :] # (D * 1 * H * W)
        return scan, mask3d.squeeze(1)
    
    #def __getitems__(self, idxs : list[int]) -> list[tuple[torch.Tensor, torch.Tensor]]:
        return
    
    def grayscale_to_rgb(self, scan: np.ndarray[float], cmap : str = 'grey') -> np.ndarray[float]:
        '''
        Colours a greyscale intensity plot.

        Args:
            scan (np.ndarray): Input greyscale scan array (D * H * W).
            cmap (string): Choice of colormap, out of the matplotlib strings - grey, bone, viridis, plasma, inferno etc...
        
        Returns:
            scan (np.ndarray): Output RGB scan array (D * H * W * 3).
        '''
        # Normalize to [0, 1] range first
        scan_norm = (scan - scan.min()) / (scan.max() - scan.min() + 1e-8)

        if cmap == 'grey':
            # Simple fast approximation - can be replaced with lookup table for production
            scan_rgb = np.stack([scan_norm, scan_norm, scan_norm], axis=-1) * 255
        else:
            # Fallback to matplotlib for other colormaps
            cmap_func = plt.get_cmap(cmap)
            scan_rgb = cmap_func(scan_norm) * 255 # (D * H * W * 4)
            scan_rgb = scan_rgb[..., :3]  # Remove alpha channel (D * H * W * 3)
        
        return scan_rgb
    
class Combined_Loss(nn.Module):
    '''
    A custom loss function class for a combined cross-entropy and DICE loss.
    '''
    def __init__(self, device, alpha : float = 1., beta : float = 0.7, gamma : float = 0.75,
                 epsilon : float = 1e-8, ce_weights : Sequence[float] = [0.5, 0.5]):
        '''
        Initialise the Combined_Loss daughter class of torch.nn.Module.

        Args:
            alpha (float): Relative weighting of Cross-Entropy and Dice losses (N.B. only relative magnitude matters, i.e. alpha = 0 -> Cross-Entropy loss dominates).
            beta (float): Relative weighting of false positives and false negatives in Tversky loss (i.e. beta = 0 -> no false negatives, beta = 1 -> no false positives).
            gamma (float): The Focal loss exponent.
            epsilon (float): The smoothing factor.
            ce_weights (iterable): The relative weighting of classes within the Cross-Entropy loss.
        '''
        super().__init__()
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self.epsilon = epsilon
        self.CELoss = nn.CrossEntropyLoss(weight = torch.tensor(ce_weights).to(device))

    def forward(self, output : torch.Tensor, target : torch.Tensor) -> float:
        '''
        Calculates the combined cross-entropy and DICE loss, i.e. forward pass of combined loss function.
        
        Args:
            output (torch.Tensor): Predicted mask logits Float32 tensor (D * 2 * H * W).
            target (torch.Tensor): Ground truth binary (0,1) mask Int64 tensor (D * H * W).
        
        Returns:
            loss (float): The combined loss.
        '''
        CE = self.CELoss(output, target)
        DICE = self.DiceLoss(output, target, self.epsilon)
        FOC_TVSKY = self.FocalTverskyLoss(output, target, self.epsilon)
        return self.alpha * FOC_TVSKY + CE
    
    def DiceLoss(self, output : torch.Tensor, target : torch.Tensor, epsilon : float = 1e-8) -> float:
        '''
        Calculate the Dice loss of a predicted mask with respect to the ground truth.

        Args:
            output (torch.Tensor): Predicted mask logits Float32 tensor (D * 2 * H * W).
            target (torch.Tensor): Ground truth binary (0,1) mask Int64 tensor (D * H * W).
            epsilon (float): The smoothing factor.
        
        Returns:
            dice_loss (float): The Dice loss (1 - Dice coefficient). The negation allows for gradient descent.
        '''
        # Softmax the logits to probabilities
        probs = torch.softmax(output, dim=1)

        # Create one-hot encoding
        target_onehot = torch.zeros_like(output)
        target_onehot.scatter_(1, target.unsqueeze(1), 1)
        
        # Focus on foreground class (class 1)
        probs_fg = probs[:, 1]
        target_fg = target_onehot[:, 1]
        
        # Calculate intersection and union in one pass
        intersection = (probs_fg * target_fg).sum(dim=(1, 2))
        probs_sum = probs_fg.sum(dim=(1, 2))
        target_sum = target_fg.sum(dim=(1, 2))
        
        dice_coeff = (2 * intersection + epsilon) / (probs_sum + target_sum + epsilon)
        dice_loss = 1 - dice_coeff
        return dice_loss.mean() # Average over depth

    def FocalTverskyLoss(self, output: torch.Tensor, target : torch.Tensor, epsilon : float = 1e-8) -> float:
        '''
        Calculate the Focal Tversky loss of a predicted mask with respect to the ground truth.

        Args:
            output (torch.Tensor): Predicted mask logits Float32 tensor (D * 2 * H * W).
            target (torch.Tensor): Ground truth binary (0,1) mask Int64 tensor (D * H * W).
            epsilon (float): The smoothing factor.
        
        Returns:
            focal_tversky_loss (float): The Focal Tversky loss.
        '''
        # Hyperparameters
        alpha = 1 - self.beta
        beta = self.beta
        gamma = self.gamma
        
        # Softmax the logits to probabilities
        probs = torch.softmax(output, dim=1)
        
        # Create one-hot encoding
        target_onehot = torch.zeros_like(output)
        target_onehot.scatter_(1, target.unsqueeze(1), 1)
        
        # Focus on foreground class (class 1)
        probs_fg = probs[:, 1]
        target_fg = target_onehot[:, 1]
        
        # Calculate TP, FP, FN and Loss function
        TP = (probs_fg * target_fg).sum(dim=(1, 2))
        FP = (probs_fg * (1 - target_fg)).sum(dim=(1, 2))
        FN = ((1 - probs_fg) * target_fg).sum(dim=(1, 2))
        
        tversky_coeff = (TP + epsilon) / (TP + alpha * FP + beta * FN + epsilon)
        focal_tversky_loss = (1 - tversky_coeff) ** gamma
        return focal_tversky_loss.mean() # Average over depth

def grayscale_to_rgb(scan : np.ndarray[float], cmap : str = 'inferno') -> np.ndarray[float]:
    '''
    Colours a greyscale intensity plot.

    Args:
        scan (np.ndarray): Input greyscale scan array (D * H * W).
        cmap (string): Choice of colormap, out of the matplotlib strings - grey, bone, viridis, plasma, inferno etc...

    Returns:
        scan (np.ndarray): Output RGB scan array (D * H * W * 3).
    '''
    # Normalize to [0, 1] range first
    scan_norm = (scan - scan.min()) / (scan.max() - scan.min() + 1e-8)

    if cmap == 'inferno' or cmap == 'viridis':
        # Simple fast approximation - can be replaced with lookup table for production
        scan_rgb = np.stack([scan_norm, scan_norm, scan_norm], axis=-1) * 255
    else:
        # Fallback to matplotlib for other colormaps
        cmap_func = plt.get_cmap(cmap)
        scan_rgb = cmap_func(scan_norm) * 255 # (D * H * W * 4)
        scan_rgb = scan_rgb[..., :3]  # Remove alpha channel (D * H * W * 3)
    
    return scan_rgb

def clip_and_scale(tensor : torch.Tensor, low_clip : float = 1., high_clip : float = 99.) -> torch.Tensor:
    '''
    Normalize a torch tensor image by clipping and scaling to [0, 1].

    Args:
        tensor (torch.Tensor): Input image Float32 tensor (any shape).
        low_clip (float): Lower percentile to clip at.
        high_clip (float): Upper percentile to clip at.

    Returns:
        tensor (torch.Tensor): Normalized Float32 tensor scaled to [0, 1].
    '''
    # Clip
    flat = tensor.flatten()
    lower = torch.quantile(flat, low_clip / 100)
    upper = torch.quantile(flat, high_clip / 100)
    tensor = torch.clamp(tensor, min = lower, max = upper)

    # Scale to [0, 1]
    tensor = (tensor - tensor.min()) / (tensor.max() - tensor.min() + 1e-8)
    return tensor

def sum_IoU(pred_mask : torch.Tensor, true_mask : torch.Tensor) -> float:
    '''
    Calculate the Intersection over Union (IoU) value between the predicted mask and ground truth.

    Args:
        pred_mask (torch.Tensor): A binary (0,1) predicted mask Int64 tensor (any shape).
        true_mask (torch.Tensor): A binary (0,1) ground truth mask Int64 tensor (same shape as pred_mask)
    
    Returns:
        IoU (float): The IoU score of the two mask tensors.
    '''
    pred_bool = pred_mask.bool()
    true_bool = true_mask.bool()
    
    intersection = (pred_bool & true_bool).sum().item()
    union = (pred_bool | true_bool).sum().item()

    if union == 0: # Handle empty masks
        return 1.0 if intersection == 0 else 0.0
    return float(intersection) / union

def get_model_instance_segmentation(num_classes : int, device : str = 'cpu', trained : bool = False) -> torchvision.models.segmentation.fcn.FCN:
    '''
    Load an instance of the FCN ResNet 101 model, with pre-trained weights.

    Args:
        num_classes (int): The number of output classes. Here, we use two -> 0. Background | 1. Blood vessel
        trained (bool): A boolean depicting whether the model has been locally trained or not, i.e. whether to load fine-tuned or default weights.

    Returns:
        model (torchvision.models.segmentation.fcn_resnet101): The model with required weights.
    '''
    # Load the instance segmentation model pre=trained on COCO
    model = torchvision.models.segmentation.fcn_resnet101(weights = "COCO_WITH_VOC_LABELS_V1")

    # Replace the classifier with a new one, that has a user defined num_classes
    # Get the number of input features for the final layer of the ResNet
    inter_channels = model.classifier[4].in_channels
    # Replace the final convolutional layer with a new one
    model.classifier[4] = nn.Conv2d(inter_channels, num_classes, kernel_size = 1)

    if trained: # If the model has been locally trained, load the fine-tuned weights
        model.load_state_dict(torch.load('model_params.pth', weights_only = True))

    return model.to(device) # Move the model to the specified device

def get_transform(data: str = 'target', phase: str = 'train') -> T.Compose:
    '''
    Get the appropriate transform for the input data.
    Args:
        phase (str): The phase of the dataset, either 'train' or 'val'.
        data (str): The type of input data, either 'input' for scans or 'target' for masks.
    Returns:
        transform (T.Compose): The composed transform for the specified input type.
    '''
    # Note: BILINEAR for images (smooth), NEAREST for masks (preserve labels)
    if data == 'target':
        interpolation = T.InterpolationMode.NEAREST
        # For masks: don't scale, keep as integers for proper class labels
        return T.Compose([
            T.ToImage(),
            T.ToDtype(torch.int64, scale=False),  # Keep integer labels, no scaling
            T.Resize(size=(50, 50), interpolation=interpolation),
        ])
    else: 
        interpolation = T.InterpolationMode.BILINEAR
        if phase == 'train':
            return T.Compose([
                T.ToImage(),
                T.ToDtype(torch.float32, scale=True),
                T.Resize(size=(50, 50), interpolation=interpolation),
                #T.RandomResizedCrop(size = (50, 50), scale = (0.5, 1.5), interpolation = interpolation),  # vary size
                T.GaussianBlur(kernel_size = 5, sigma = 0.1),
                T.Lambda(clip_and_scale)
            ])
        else:  # validation phase
            return T.Compose([
                T.ToImage(),
                T.ToDtype(torch.float32, scale=True),
                T.Resize(size=(50, 50), interpolation=interpolation),
                T.GaussianBlur(kernel_size = 5, sigma = 0.1),
                T.Lambda(clip_and_scale)
            ])

def custom_collate_fn(batch):
    """
    Custom collate function to handle variable-sized 3D scans.
    Returns the first item since we can't batch variable-sized 3D volumes
    """
    return batch[0]