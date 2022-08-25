######## lib ##########

import os
from os.path import join, isfile, isdir
from os import listdir
import argparse
from argparse import ArgumentParser
import sys
import numpy as np
import cv2
from skimage import color
import skimage.io

# import some common detectron2 utilities
import detectron2
from detectron2.utils.logger import setup_logger
from detectron2 import model_zoo
from detectron2.engine import DefaultPredictor
from detectron2.config import get_cfg
setup_logger()

import torch
from tqdm import tqdm
from PIL import Image
import shutil
import glob
import matplotlib.pyplot as plt
# os.environ["CUDA_VISIBLE_DEVICES"] = "0"

import time
import torch
import torch.backends.cudnn as cudnn
import torchvision.transforms as transform_lib
import lib.TestTransforms as transforms
from models.ColorVidNet import ColorVidNet
from models.FrameColor import frame_colorization
from models.NonlocalNet import VGG19_pytorch, WarpNet
from utils.util import (batch_lab2rgb_transpose_mc, folder2vid, mkdir_if_not,
                        save_frames, tensor_lab2rgb, uncenter_l)
from utils.util_distortion import CenterPad, Normalize, RGB2Lab, ToTensor


cfg = get_cfg()
cfg.merge_from_file(model_zoo.get_config_file("COCO-InstanceSegmentation/mask_rcnn_X_101_32x8d_FPN_3x.yaml"))
cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.7
cfg.MODEL.WEIGHTS = model_zoo.get_checkpoint_url("COCO-InstanceSegmentation/mask_rcnn_X_101_32x8d_FPN_3x.yaml")
predictor = DefaultPredictor(cfg)

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "1"
torch.cuda.set_device(0)


class args:
    test_img_dir = "./sample_videos/clips/taxi" # input image 경로 (crop 할 이미지 원본 경로)
    filter_no_obj = False
    test_img = "41.jpg"
    cropped_dir = "./sample_videos/frames" 
    segmented = True
    taxi = True
    
    
def read_to_pil(img_path):
    '''
    return: pillow image object HxWx3
    '''
    out_img = Image.open(img_path)
    if len(np.asarray(out_img).shape) == 2:
        out_img = np.stack([np.asarray(out_img), np.asarray(out_img), np.asarray(out_img)], 2)
        out_img = Image.fromarray(out_img)
    return out_img

def colorize_video(opt, input_path, reference_file, output_path, nonlocal_net, colornet, vggnet):
    # parameters for wls filter
    wls_filter_on = True
    lambda_value = 500
    sigma_color = 4

    # processing folders
    mkdir_if_not(output_path)
    files = glob.glob(output_path + "*")
    print("processing the folder:", input_path)
    path, dirs, filenames = os.walk(input_path).__next__()
    file_count = len(filenames)
    filenames.sort(key=lambda f: int("".join(filter(str.isdigit, f) or -1)))

    # NOTE: resize frames to 216*384
    # transform = transforms.Compose(
    #     [CenterPad(opt.image_size), transform_lib.CenterCrop(opt.image_size), RGB2Lab(), ToTensor(), Normalize()]
    # )

    transform = transforms.Compose(
        [transform_lib.Resize(opt.image_size), RGB2Lab(), ToTensor(), Normalize()]
    )
    
    # if frame propagation: use the first frame as reference
    # otherwise, use the specified reference image
    ref_name = input_path + filenames[0] if opt.frame_propagate else reference_file
    print("reference name:", ref_name)
    frame_ref = Image.open(ref_name)
    frame_ref = frame_ref.convert("RGB") ####

    total_time = 0
    I_last_lab_predict = None

    IB_lab_large = transform(frame_ref).unsqueeze(0).cuda()

    IB_lab = torch.nn.functional.interpolate(IB_lab_large, scale_factor=0.5, mode="bilinear")
    IB_l = IB_lab[:, 0:1, :, :]
    IB_ab = IB_lab[:, 1:3, :, :]
    with torch.no_grad():
      I_reference_lab = IB_lab
      I_reference_l = I_reference_lab[:, 0:1, :, :]
      I_reference_ab = I_reference_lab[:, 1:3, :, :]
      I_reference_rgb = tensor_lab2rgb(torch.cat((uncenter_l(I_reference_l), I_reference_ab), dim=1))
      features_B = vggnet(I_reference_rgb, ["r12", "r22", "r32", "r42", "r52"], preprocess=True)

    for index, frame_name in enumerate(tqdm(filenames)):
        
        frame1 = Image.open(os.path.join(input_path, frame_name))
        frame1 = frame1.convert("RGB") ####
        
        IA_lab_large = transform(frame1).unsqueeze(0).cuda()

        
        IA_lab = torch.nn.functional.interpolate(IA_lab_large, scale_factor=0.5, mode="bilinear")

        IA_l = IA_lab[:, 0:1, :, :]
        IA_ab = IA_lab[:, 1:3, :, :]

        
        if I_last_lab_predict is None:
            if opt.frame_propagate:
                I_last_lab_predict = IB_lab
            else:
                I_last_lab_predict = torch.zeros_like(IA_lab).cuda()

        # start the frame colorization
        with torch.no_grad():
            I_current_lab = IA_lab
            I_current_ab_predict, I_current_nonlocal_lab_predict, features_current_gray = frame_colorization(
                I_current_lab,
                I_reference_lab,
                I_last_lab_predict,
                features_B,
                vggnet,
                nonlocal_net,
                colornet,
                feature_noise=0,
                temperature=1e-10,
            )
            I_last_lab_predict = torch.cat((IA_l, I_current_ab_predict), dim=1)

        # upsampling
        curr_bs_l = IA_lab_large[:, 0:1, :, :]
        curr_predict = (
            torch.nn.functional.interpolate(I_current_ab_predict.data.cpu(), scale_factor=2, mode="bilinear") * 1.25
        )

        # filtering
        if wls_filter_on:
            guide_image = uncenter_l(curr_bs_l) * 255 / 100
            wls_filter = cv2.ximgproc.createFastGlobalSmootherFilter(
                guide_image[0, 0, :, :].cpu().numpy().astype(np.uint8), lambda_value, sigma_color
            )
            curr_predict_a = wls_filter.filter(curr_predict[0, 0, :, :].cpu().numpy())
            curr_predict_b = wls_filter.filter(curr_predict[0, 1, :, :].cpu().numpy())
            curr_predict_a = torch.from_numpy(curr_predict_a).unsqueeze(0).unsqueeze(0)
            curr_predict_b = torch.from_numpy(curr_predict_b).unsqueeze(0).unsqueeze(0)
            curr_predict_filter = torch.cat((curr_predict_a, curr_predict_b), dim=1)
            IA_predict_rgb = batch_lab2rgb_transpose_mc(curr_bs_l[:32], curr_predict_filter[:32, ...])
        else:
            IA_predict_rgb = batch_lab2rgb_transpose_mc(curr_bs_l[:32], curr_predict[:32, ...])

        # save the frames
        # save_frames(IA_predict_rgb, output_path, index)
        save_frames(IA_predict_rgb, output_path, image_name = frame_name)


input_dir = args.test_img_dir
image_list = [f for f in listdir(input_dir) if isfile(join(input_dir, f))]

for image_path in tqdm(image_list):
    
    img = cv2.imread(join(input_dir, image_path))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    lab_image = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab_image)
    l_stack = np.stack([l_channel, l_channel, l_channel], axis=2)
    outputs = predictor(l_stack)
    
    if args.taxi:
        outputs["instances"] = outputs["instances"][outputs["instances"].pred_classes == 2] ### CAR only
    # print(outputs['instances'].pred_classes)
    # print(outputs["instances"].scores)
    pred_bbox = outputs["instances"].pred_boxes.to(torch.device('cpu')).tensor.numpy()
    pred_scores = outputs["instances"].scores.cpu().data.numpy()
    
    
    ## 1단계 전체 이미지 segmentation
    mask = outputs["instances"].pred_masks
    
    if args.segmented:
        mask1 = mask.detach().cpu().numpy().astype(int)
        mask2 = np.where(mask1.sum(axis=0)>0, 1, 0)
        mask3 = np.stack([mask2, mask2, mask2], axis=2) # mask 3개 모두 사용 

        mask4 = np.where(mask3==0, 1, 0)

        original_img = img * mask4 # original에서 segmented부분 뺀 사진
        seg_img = img * mask3 # 검정색 배경 segmented
        seg_img_2 = np.where(seg_img==0, 255, seg_img) # 흰색 배경
        black_mask = np.where(seg_img==0, 255, 0)
        
        # plt.imshow(seg_img_2)
        skimage.io.imsave("./visualize/ori_white_mask.png",seg_img_2) # ori_white_mask
        skimage.io.imsave("./visualize/black_mask.png",black_mask) # black_mask
        skimage.io.imsave(f"./{args.cropped_dir}/target_0.png",seg_img) # ori_black_mask -> colorize할 것
        skimage.io.imsave(f"./visualize/original_img.png",original_img) # ori_black_mask -> colorize할 것


# os.chdir("./examplar")
# os.system(f"python test.py --clip_path ./sample_videos/frames \
#    --ref_path ./sample_videos/ref/taxi \
#    --output_path ./sample_videos/output")

# clip_path="./examplar/sample_videos/frames"
# ref_path="./examplar/sample_videos/ref/taxi"
# output_path="./examplar/sample_videos/output"


parser = argparse.ArgumentParser()
parser.add_argument(
    "--frame_propagate", default=False, type=bool, help="propagation mode, , please check the paper"
)
parser.add_argument("--image_size", type=int, default=[216 * 2, 384 * 2], help="the image size, eg. [216,384]")
parser.add_argument("--cuda", action="store_false")
parser.add_argument("--gpu_ids", type=str, default="0", help="separate by comma")
parser.add_argument("--clip_path", type=str, default="./sample_videos/frames", help="path of input clips")
parser.add_argument("--ref_path", type=str, default="./sample_videos/ref/taxi", help="path of refernce images")
parser.add_argument("--output_path", type=str, default="./sample_videos/output", help="path of output clips")
opt = parser.parse_args()
opt.gpu_ids = [int(x) for x in opt.gpu_ids.split(",")]
cudnn.benchmark = True
print("running on GPU", opt.gpu_ids)

clip_name = opt.clip_path.split("/")[-1]
refs = os.listdir(opt.ref_path)
refs.sort()

nonlocal_net = WarpNet(1)
colornet = ColorVidNet(7)
vggnet = VGG19_pytorch()
vggnet.load_state_dict(torch.load("data/vgg19_conv.pth"))
for param in vggnet.parameters():
    param.requires_grad = False

nonlocal_test_path = os.path.join("checkpoints/", "video_moredata_l1/nonlocal_net_iter_76000.pth")
color_test_path = os.path.join("checkpoints/", "video_moredata_l1/colornet_iter_76000.pth")
print("succesfully load nonlocal model: ", nonlocal_test_path)
print("succesfully load color model: ", color_test_path)
nonlocal_net.load_state_dict(torch.load(nonlocal_test_path))
colornet.load_state_dict(torch.load(color_test_path))

nonlocal_net.eval()
colornet.eval()
vggnet.eval()
nonlocal_net.cuda()
colornet.cuda()
vggnet.cuda()

# 여기서 reference 사진 받아오는 코드
# 원본 input 사진을 sample_videos/clips/taxi 안에 넣기
# 나머지 사진은 sample_videos/ref/taxi 안에 넣기

for ref_name in refs:
    try:
        colorize_video(
            opt,
            opt.clip_path,
            os.path.join(opt.ref_path, ref_name),
            os.path.join(opt.output_path, clip_name + "_" + ref_name.split(".")[0]),
            nonlocal_net,
            colornet,
            vggnet,
        )
    except Exception as error:
        print("error when colorizing the video " + ref_name)
        print(error)    


# 합치기
img = Image.open(f"./visualize/original_img.png")
img2 = Image.open(f"./sample_videos/output/frames_ref/target_0.png")

im = img.resize((768, 432))

original = np.array(im)
seg = np.array(img2)
converted = original+seg

skimage.io.imsave("./visualize/converted.png",converted) # original_img 저장