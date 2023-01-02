import os.path

import torch
import cv2
from regressors.build_net import build_network
from lanegnn.utils import visualize_angles
from glob import glob
from PIL import Image
import argparse
from tqdm import tqdm


def get_id(filename):
    return '-'.join(os.path.basename(filename).split('-')[0:3])

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out_path_root', type=str, default='')
    parser.add_argument('--checkpoint', type=str, default='../checkpoints/regressor-newest.pth')
    args = parser.parse_args()

    regressor = build_network(snapshot=None, backend='resnet101', num_channels=3, n_classes=3).cuda()

    state_dict = torch.load(args.checkpoint)
    for key in list(state_dict.keys()):
        state_dict[key.replace('module.', '')] = state_dict[key]
        del state_dict[key]

    regressor.load_state_dict(state_dict)
    regressor.eval()

    sat_images = sorted(glob(os.path.join(args.out_path_root, "train", "*-rgb.png")) +
                        glob(os.path.join(args.out_path_root, "val", "*-rgb.png")))

    for sat_image_f in tqdm(sat_images):
        rgb = cv2.imread(sat_image_f)

        out_path = os.path.dirname(sat_image_f)
        sample_id = get_id(sat_image_f)

        #rgb = cv2.cvtColor(sat_image, cv2.COLOR_RGB2BGR)
        pred = regressor(torch.FloatTensor(rgb).permute(2, 0, 1).unsqueeze(0).cuda() / 255.)
        sdf = torch.nn.Sigmoid()(pred[0, 2]).detach().cpu().numpy()
        angles = torch.nn.Tanh()(pred[0, 0:2]).detach().cpu().numpy()

        angles_viz = visualize_angles(angles[0], angles[1], mask=sdf)

        Image.fromarray(angles_viz).save("{}/{}-angles-reg.png".format(out_path, sample_id))
        Image.fromarray(sdf * 255.).convert("L").save("{}/{}-sdf-reg.png".format(out_path, sample_id))