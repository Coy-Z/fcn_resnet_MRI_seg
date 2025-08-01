import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import numpy as np
import scipy
import torch
from torchvision.transforms import v2 as T
from utils.fcn_resnet101_util import clip_and_scale, get_model_instance_segmentation, grayscale_to_rgb, get_transform

val_transform = T.Compose([
    T.ToImage(),
    T.ToDtype(torch.float32, scale=True),
    T.Resize(size=(50, 50), interpolation=T.InterpolationMode.BILINEAR),
    T.Lambda(clip_and_scale)
])

def evaluation(model, scan, device):
    '''
    Calculates the mask.
    Args
        model: the model being used
        images: a numpy array of pixel values (single channel, i.e. greyscale)
        device: the device being used
    Returns
        masks: a tensor of masks, the same shape as the images array
    '''
    if model.training:
        model.eval()
    with torch.inference_mode():
        slices = grayscale_to_rgb(scan)
        inputs = torch.stack([val_transform(slice) for slice in slices]).to(device)
        outputs = model(inputs)
        preds = torch.argmax(outputs['out'], dim = 1)
    masks = preds.squeeze().cpu()
    return masks

# Device and model setup
device = torch.accelerator.current_accelerator().type if torch.accelerator.is_available() else "cpu"
model = get_model_instance_segmentation(num_classes = 2, trained = True)
model.to(device)

# Load data: Optionally apply Gaussian smoothing
#images = scipy.ndimage.gaussian_filter(np.load('data/magn/Aorta.npy'), sigma = 2)
images = np.load('data/val/magn/Aorta.npy')

# Inference
masks = evaluation(model, images, device)

# Visualization
pcm = []
fig, ax = plt.subplots(1, 2, figsize = (10,6))
pcm.append(ax[0].imshow(images[13], cmap = 'bone'))
pcm.append(ax[1].imshow(masks[13], cmap = 'cividis', vmin=0, vmax=1))
fig.colorbar(pcm[1], ax = ax, shrink = 0.6)
ax[0].axis("off")
ax[1].axis("off")

# Animation update function
def updateAnim(frame):
    pcm[0].set_data(images[frame])
    pcm[1].set_data(masks[frame])
    return pcm

# Create animation and keep reference to prevent garbage collection
ani = FuncAnimation(fig, updateAnim, frames = images.shape[0], interval = 100, blit = False)

# Save animation as GIF to prevent the warning and ensure it's properly rendered
print("Saving animation as GIF...")
ani.save('images/segmentation_animation.gif', writer='pillow', fps=10)
print("Animation saved as segmentation_animation.gif")

# Optionally show the plot if display is available
try:
    plt.show(block=True)
except:
    print("Display not available, animation saved to file instead")