# pylint: disable=no-member,invalid-name,line-too-long,missing-function-docstring,missing-class-docstring,too-many-branches
# pylint: disable=too-many-locals,wrong-import-position,too-many-lines,too-many-statements,chained-comparison,fixme,broad-except,c-extension-no-member
# pylint: disable=too-many-public-methods,too-many-arguments,too-many-instance-attributes,too-many-public-methods,
# pylint: disable=consider-using-enumerate
"""
tool to extract table form data from alto xml data
"""

import math
import xml.etree.ElementTree as ET
import os
import sys
import time
import warnings
from pathlib import Path
from multiprocessing import Process, Queue, cpu_count
import gc
import cv2
import numpy as np
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
stderr = sys.stderr
sys.stderr = open(os.devnull, "w")
from ocrd_utils import getLogger, tf_disable_interactive_logs
sys.stderr = stderr
import tensorflow as tf
from tensorflow.python.keras import backend as K
from tensorflow.keras.models import load_model
tf.get_logger().setLevel("ERROR")
warnings.filterwarnings("ignore")
from scipy.signal import find_peaks
#import matplotlib.pyplot as plt
from scipy.ndimage import gaussian_filter1d
from tqdm import tqdm

from .utils.contour import (
    filter_contours_area_of_image,
    filter_contours_area_of_image_tables,
    find_contours_mean_y_diff,
    find_new_features_of_contours,
    find_features_of_contours,
    get_text_region_boxes_by_given_contours,
    get_textregion_contours_in_org_image,
    return_contours_of_image,
    return_contours_of_interested_region,
    return_contours_of_interested_region_by_min_size,
    return_contours_of_interested_textline,
    return_parent_contours,
)
from .utils.rotate import (
    rotate_image,
    rotation_not_90_func,
    rotation_not_90_func_full_layout)
from .utils.separate_lines import (
    textline_contours_postprocessing,
    separate_lines_new2,
    return_deskew_slop)
from .utils.drop_capitals import (
    adhere_drop_capital_region_into_corresponding_textline,
    filter_small_drop_capitals_from_no_patch_layout)
from .utils.marginals import get_marginals
from .utils.resize import resize_image
from .utils import (
    boosting_headers_by_longshot_region_segmentation,
    crop_image_inside_box,
    find_num_col,
    otsu_copy_binary,
    put_drop_out_from_only_drop_model,
    putt_bb_of_drop_capitals_of_model_in_patches_in_layout,
    check_any_text_region_in_model_one_is_main_or_header,
    small_textlines_to_parent_adherence2,
    order_of_regions,
    find_number_of_columns_in_document,
    return_boxes_of_images_by_order_of_reading_new)
from .utils.pil_cv2 import check_dpi, pil2cv
from .utils.xml import order_and_id_of_texts
from .plot import EynollahPlotter
from .writer import EynollahXmlWriter

SLOPE_THRESHOLD = 0.13
RATIO_OF_TWO_MODEL_THRESHOLD = 95.50 #98.45:
DPI_THRESHOLD = 298
MAX_SLOPE = 999
KERNEL = np.ones((5, 5), np.uint8)

class Sbb_standalone_textline:
    def __init__(
        self,
        dir_models,
        image_filename,
        image_pil=None,
        image_filename_stem=None,
        dir_out=None,
        logger=None,
        pcgts=None,
    ):
        self.image_filename = image_filename
        print(Path(image_filename).name)
        self.image_filename_stem=Path(Path(image_filename).name).stem
        self.dir_out = dir_out
        self.dir_models = dir_models
        self.logger = logger if logger else getLogger('eynollah')
        self.model_dir_of_binarization = dir_models + "/model_bin_sbb_ens"
        self.model_textline_dir = dir_models + "/model_hand_ens2022_9^211"
        if image_pil:
            self._imgs = self._cache_images(image_pil=image_pil)
        else:
            self._imgs = self._cache_images(image_filename=image_filename)
        
    def _cache_images(self, image_filename=None, image_pil=None):
        ret = {}
        if image_filename:
            ret['img'] = cv2.imread(image_filename)
            self.dpi = check_dpi(image_filename)
        else:
            ret['img'] = pil2cv(image_pil)
            self.dpi = check_dpi(image_pil)
        ret['img_grayscale'] = cv2.cvtColor(ret['img'], cv2.COLOR_BGR2GRAY)
        for prefix in ('',  '_grayscale'):
            ret[f'img{prefix}_uint8'] = ret[f'img{prefix}'].astype(np.uint8)
        return ret

    def imread(self, grayscale=False, uint8=True):
        key = 'img'
        if grayscale:
            key += '_grayscale'
        if uint8:
            key += '_uint8'
        return self._imgs[key].copy()
    
    def isNaN(self, num):
        return num != num


    def predict_enhancement(self, img):
        self.logger.debug("enter predict_enhancement")
        model_enhancement, session_enhancement = self.start_new_session_and_model(self.model_dir_of_enhancement)

        img_height_model = model_enhancement.layers[len(model_enhancement.layers) - 1].output_shape[1]
        img_width_model = model_enhancement.layers[len(model_enhancement.layers) - 1].output_shape[2]
        if img.shape[0] < img_height_model:
            img = cv2.resize(img, (img.shape[1], img_width_model), interpolation=cv2.INTER_NEAREST)

        if img.shape[1] < img_width_model:
            img = cv2.resize(img, (img_height_model, img.shape[0]), interpolation=cv2.INTER_NEAREST)
        margin = int(0 * img_width_model)
        width_mid = img_width_model - 2 * margin
        height_mid = img_height_model - 2 * margin
        img = img / float(255.0)

        img_h = img.shape[0]
        img_w = img.shape[1]

        prediction_true = np.zeros((img_h, img_w, 3))
        nxf = img_w / float(width_mid)
        nyf = img_h / float(height_mid)

        nxf = int(nxf) + 1 if nxf > int(nxf) else int(nxf)
        nyf = int(nyf) + 1 if nyf > int(nyf) else int(nyf)

        for i in range(nxf):
            for j in range(nyf):
                if i == 0:
                    index_x_d = i * width_mid
                    index_x_u = index_x_d + img_width_model
                else:
                    index_x_d = i * width_mid
                    index_x_u = index_x_d + img_width_model
                if j == 0:
                    index_y_d = j * height_mid
                    index_y_u = index_y_d + img_height_model
                else:
                    index_y_d = j * height_mid
                    index_y_u = index_y_d + img_height_model

                if index_x_u > img_w:
                    index_x_u = img_w
                    index_x_d = img_w - img_width_model
                if index_y_u > img_h:
                    index_y_u = img_h
                    index_y_d = img_h - img_height_model

                img_patch = img[index_y_d:index_y_u, index_x_d:index_x_u, :]
                label_p_pred = model_enhancement.predict(img_patch.reshape(1, img_patch.shape[0], img_patch.shape[1], img_patch.shape[2]))

                seg = label_p_pred[0, :, :, :]
                seg = seg * 255

                if i == 0 and j == 0:
                    seg = seg[0 : seg.shape[0] - margin, 0 : seg.shape[1] - margin]
                    prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + 0 : index_x_u - margin, :] = seg
                elif i == nxf - 1 and j == nyf - 1:
                    seg = seg[margin : seg.shape[0] - 0, margin : seg.shape[1] - 0]
                    prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - 0, :] = seg
                elif i == 0 and j == nyf - 1:
                    seg = seg[margin : seg.shape[0] - 0, 0 : seg.shape[1] - margin]
                    prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + 0 : index_x_u - margin, :] = seg
                elif i == nxf - 1 and j == 0:
                    seg = seg[0 : seg.shape[0] - margin, margin : seg.shape[1] - 0]
                    prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - 0, :] = seg
                elif i == 0 and j != 0 and j != nyf - 1:
                    seg = seg[margin : seg.shape[0] - margin, 0 : seg.shape[1] - margin]
                    prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + 0 : index_x_u - margin, :] = seg
                elif i == nxf - 1 and j != 0 and j != nyf - 1:
                    seg = seg[margin : seg.shape[0] - margin, margin : seg.shape[1] - 0]
                    prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - 0, :] = seg
                elif i != 0 and i != nxf - 1 and j == 0:
                    seg = seg[0 : seg.shape[0] - margin, margin : seg.shape[1] - margin]
                    prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - margin, :] = seg
                elif i != 0 and i != nxf - 1 and j == nyf - 1:
                    seg = seg[margin : seg.shape[0] - 0, margin : seg.shape[1] - margin]
                    prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - margin, :] = seg
                else:
                    seg = seg[margin : seg.shape[0] - margin, margin : seg.shape[1] - margin]
                    prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - margin, :] = seg

        prediction_true = prediction_true.astype(int)
        session_enhancement.close()
        del model_enhancement
        del session_enhancement
        gc.collect()

        return prediction_true

    def calculate_width_height_by_columns(self, img, num_col, width_early, label_p_pred):
        self.logger.debug("enter calculate_width_height_by_columns")
        if num_col == 1 and width_early < 1100:
            img_w_new = 2000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 2000)
        elif num_col == 1 and width_early >= 2500:
            img_w_new = 2000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 2000)
        elif num_col == 1 and width_early >= 1100 and width_early < 2500:
            img_w_new = width_early
            img_h_new = int(img.shape[0] / float(img.shape[1]) * width_early)
        elif num_col == 2 and width_early < 2000:
            img_w_new = 2400
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 2400)
        elif num_col == 2 and width_early >= 3500:
            img_w_new = 2400
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 2400)
        elif num_col == 2 and width_early >= 2000 and width_early < 3500:
            img_w_new = width_early
            img_h_new = int(img.shape[0] / float(img.shape[1]) * width_early)
        elif num_col == 3 and width_early < 2000:
            img_w_new = 3000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 3000)
        elif num_col == 3 and width_early >= 4000:
            img_w_new = 3000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 3000)
        elif num_col == 3 and width_early >= 2000 and width_early < 4000:
            img_w_new = width_early
            img_h_new = int(img.shape[0] / float(img.shape[1]) * width_early)
        elif num_col == 4 and width_early < 2500:
            img_w_new = 4000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 4000)
        elif num_col == 4 and width_early >= 5000:
            img_w_new = 4000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 4000)
        elif num_col == 4 and width_early >= 2500 and width_early < 5000:
            img_w_new = width_early
            img_h_new = int(img.shape[0] / float(img.shape[1]) * width_early)
        elif num_col == 5 and width_early < 3700:
            img_w_new = 5000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 5000)
        elif num_col == 5 and width_early >= 7000:
            img_w_new = 5000
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 5000)
        elif num_col == 5 and width_early >= 3700 and width_early < 7000:
            img_w_new = width_early
            img_h_new = int(img.shape[0] / float(img.shape[1]) * width_early)
        elif num_col == 6 and width_early < 4500:
            img_w_new = 6500  # 5400
            img_h_new = int(img.shape[0] / float(img.shape[1]) * 6500)
        else:
            img_w_new = width_early
            img_h_new = int(img.shape[0] / float(img.shape[1]) * width_early)

        if label_p_pred[0][int(num_col - 1)] < 0.9 and img_w_new < width_early:
            img_new = np.copy(img)
            num_column_is_classified = False
        else:
            img_new = resize_image(img, img_h_new, img_w_new)
            num_column_is_classified = True

        return img_new, num_column_is_classified

    def resize_image_with_column_classifier(self, is_image_enhanced, img_bin):
        self.logger.debug("enter resize_image_with_column_classifier")
        if self.input_binary:
            img = np.copy(img_bin)
        else:
            img = self.imread()

        _, page_coord = self.early_page_for_num_of_column_classification(img)
        model_num_classifier, session_col_classifier = self.start_new_session_and_model(self.model_dir_of_col_classifier)
        if self.input_binary:
            img_in = np.copy(img)
            img_in = img_in / 255.0
            width_early = img_in.shape[1]
            img_in = cv2.resize(img_in, (448, 448), interpolation=cv2.INTER_NEAREST)
            img_in = img_in.reshape(1, 448, 448, 3)
        else:
            img_1ch = self.imread(grayscale=True, uint8=False)
            width_early = img_1ch.shape[1]
            img_1ch = img_1ch[page_coord[0] : page_coord[1], page_coord[2] : page_coord[3]]

            # plt.imshow(img_1ch)
            # plt.show()
            img_1ch = img_1ch / 255.0

            img_1ch = cv2.resize(img_1ch, (448, 448), interpolation=cv2.INTER_NEAREST)

            img_in = np.zeros((1, img_1ch.shape[0], img_1ch.shape[1], 3))
            img_in[0, :, :, 0] = img_1ch[:, :]
            img_in[0, :, :, 1] = img_1ch[:, :]
            img_in[0, :, :, 2] = img_1ch[:, :]

        label_p_pred = model_num_classifier.predict(img_in)
        num_col = np.argmax(label_p_pred[0]) + 1

        self.logger.info("Found %s columns (%s)", num_col, label_p_pred)

        session_col_classifier.close()
        
        del model_num_classifier
        del session_col_classifier
        
        K.clear_session()
        gc.collect()



        img_new, _ = self.calculate_width_height_by_columns(img, num_col, width_early, label_p_pred)

        if img_new.shape[1] > img.shape[1]:
            img_new = self.predict_enhancement(img_new)
            is_image_enhanced = True

        return img, img_new, is_image_enhanced

    def resize_and_enhance_image_with_column_classifier(self,light_version):
        self.logger.debug("enter resize_and_enhance_image_with_column_classifier")
        if light_version:
            dpi = 300
        else:
            dpi = self.dpi
            self.logger.info("Detected %s DPI", dpi)
        if self.input_binary:
            img = self.imread()
            model_bin, session_bin = self.start_new_session_and_model(self.model_dir_of_binarization)
            prediction_bin = self.do_prediction(True, img, model_bin)
            
            prediction_bin=prediction_bin[:,:,0]
            prediction_bin = (prediction_bin[:,:]==0)*1
            prediction_bin = prediction_bin*255
            
            prediction_bin =np.repeat(prediction_bin[:, :, np.newaxis], 3, axis=2)

            session_bin.close()
            del model_bin
            del session_bin
            gc.collect()
            
            prediction_bin = prediction_bin.astype(np.uint8)
            img= np.copy(prediction_bin)
            img_bin = np.copy(prediction_bin)
        else:
            img = self.imread()
            img_bin = None

        _, page_coord = self.early_page_for_num_of_column_classification(img_bin)
        model_num_classifier, session_col_classifier = self.start_new_session_and_model(self.model_dir_of_col_classifier)
        
        if self.input_binary:
            img_in = np.copy(img)
            width_early = img_in.shape[1]
            img_in = img_in / 255.0
            img_in = cv2.resize(img_in, (448, 448), interpolation=cv2.INTER_NEAREST)
            img_in = img_in.reshape(1, 448, 448, 3)
        else:
            img_1ch = self.imread(grayscale=True)
            width_early = img_1ch.shape[1]
            img_1ch = img_1ch[page_coord[0] : page_coord[1], page_coord[2] : page_coord[3]]

            img_1ch = img_1ch / 255.0
            img_1ch = cv2.resize(img_1ch, (448, 448), interpolation=cv2.INTER_NEAREST)
            img_in = np.zeros((1, img_1ch.shape[0], img_1ch.shape[1], 3))
            img_in[0, :, :, 0] = img_1ch[:, :]
            img_in[0, :, :, 1] = img_1ch[:, :]
            img_in[0, :, :, 2] = img_1ch[:, :]



        label_p_pred = model_num_classifier.predict(img_in)
        num_col = np.argmax(label_p_pred[0]) + 1
        self.logger.info("Found %s columns (%s)", num_col, label_p_pred)
        session_col_classifier.close()
        K.clear_session()

        if dpi < DPI_THRESHOLD:
            img_new, num_column_is_classified = self.calculate_width_height_by_columns(img, num_col, width_early, label_p_pred)
            image_res = self.predict_enhancement(img_new)
            is_image_enhanced = True
        else:
            num_column_is_classified = True
            image_res = np.copy(img)
            is_image_enhanced = False

        session_col_classifier.close()

        
        self.logger.debug("exit resize_and_enhance_image_with_column_classifier")
        return is_image_enhanced, img, image_res, num_col, num_column_is_classified, img_bin

    # pylint: disable=attribute-defined-outside-init
    def get_image_and_scales(self, img_org, img_res, scale):
        self.logger.debug("enter get_image_and_scales")
        self.image = np.copy(img_res)
        self.image_org = np.copy(img_org)
        self.height_org = self.image.shape[0]
        self.width_org = self.image.shape[1]

        self.img_hight_int = int(self.image.shape[0] * scale)
        self.img_width_int = int(self.image.shape[1] * scale)
        self.scale_y = self.img_hight_int / float(self.image.shape[0])
        self.scale_x = self.img_width_int / float(self.image.shape[1])

        self.image = resize_image(self.image, self.img_hight_int, self.img_width_int)

        # Also set for the plotter
        if self.plotter:
            self.plotter.image_org = self.image_org
            self.plotter.scale_y = self.scale_y
            self.plotter.scale_x = self.scale_x
        # Also set for the writer
        self.writer.image_org = self.image_org
        self.writer.scale_y = self.scale_y
        self.writer.scale_x = self.scale_x
        self.writer.height_org = self.height_org
        self.writer.width_org = self.width_org

    def get_image_and_scales_after_enhancing(self, img_org, img_res):
        self.logger.debug("enter get_image_and_scales_after_enhancing")
        self.image = np.copy(img_res)
        self.image = self.image.astype(np.uint8)
        self.image_org = np.copy(img_org)
        self.height_org = self.image_org.shape[0]
        self.width_org = self.image_org.shape[1]

        self.scale_y = img_res.shape[0] / float(self.image_org.shape[0])
        self.scale_x = img_res.shape[1] / float(self.image_org.shape[1])

        # Also set for the plotter
        if self.plotter:
            self.plotter.image_org = self.image_org
            self.plotter.scale_y = self.scale_y
            self.plotter.scale_x = self.scale_x
        # Also set for the writer
        self.writer.image_org = self.image_org
        self.writer.scale_y = self.scale_y
        self.writer.scale_x = self.scale_x
        self.writer.height_org = self.height_org
        self.writer.width_org = self.width_org

    def start_new_session_and_model_old(self, model_dir):
        self.logger.debug("enter start_new_session_and_model (model_dir=%s)", model_dir)
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True

        session = tf.InteractiveSession()
        model = load_model(model_dir, compile=False)

        return model, session

    
    def start_new_session_and_model(self, model_dir):
        self.logger.debug("enter start_new_session_and_model (model_dir=%s)", model_dir)
        gpu_options = tf.compat.v1.GPUOptions(allow_growth=True)
        #gpu_options = tf.compat.v1.GPUOptions(per_process_gpu_memory_fraction=7.7, allow_growth=True)
        session = tf.compat.v1.Session(config=tf.compat.v1.ConfigProto(gpu_options=gpu_options))
        model = load_model(model_dir, compile=False)

        return model, session

    def do_prediction(self, patches, img, model, marginal_of_patch_percent=0.1,handwrittens = False):
        self.logger.debug("enter do_prediction")

        img_height_model = model.layers[len(model.layers) - 1].output_shape[1]
        img_width_model = model.layers[len(model.layers) - 1].output_shape[2]

        if not patches:
            img_h_page = img.shape[0]
            img_w_page = img.shape[1]
            img = img / float(255.0)
            img = resize_image(img, img_height_model, img_width_model)

            label_p_pred = model.predict(img.reshape(1, img.shape[0], img.shape[1], img.shape[2]))

            seg = np.argmax(label_p_pred, axis=3)[0]
            seg_color = np.repeat(seg[:, :, np.newaxis], 3, axis=2)
            prediction_true = resize_image(seg_color, img_h_page, img_w_page)
            prediction_true = prediction_true.astype(np.uint8)


        else:
            if img.shape[0] < img_height_model:
                img = resize_image(img, img_height_model, img.shape[1])

            if img.shape[1] < img_width_model:
                img = resize_image(img, img.shape[0], img_width_model)

            self.logger.info("Image dimensions: %sx%s", img_height_model, img_width_model)
            margin = int(marginal_of_patch_percent * img_height_model)
            width_mid = img_width_model - 2 * margin
            height_mid = img_height_model - 2 * margin
            img = img / float(255.0)
            img = img.astype(np.float16)
            img_h = img.shape[0]
            img_w = img.shape[1]
            prediction_true = np.zeros((img_h, img_w, 3))
            mask_true = np.zeros((img_h, img_w))
            nxf = img_w / float(width_mid)
            nyf = img_h / float(height_mid)
            nxf = int(nxf) + 1 if nxf > int(nxf) else int(nxf)
            nyf = int(nyf) + 1 if nyf > int(nyf) else int(nyf)

            for i in range(nxf):
                for j in range(nyf):
                    if i == 0:
                        index_x_d = i * width_mid
                        index_x_u = index_x_d + img_width_model
                    else:
                        index_x_d = i * width_mid
                        index_x_u = index_x_d + img_width_model
                    if j == 0:
                        index_y_d = j * height_mid
                        index_y_u = index_y_d + img_height_model
                    else:
                        index_y_d = j * height_mid
                        index_y_u = index_y_d + img_height_model
                    if index_x_u > img_w:
                        index_x_u = img_w
                        index_x_d = img_w - img_width_model
                    if index_y_u > img_h:
                        index_y_u = img_h
                        index_y_d = img_h - img_height_model

                    img_patch = img[index_y_d:index_y_u, index_x_d:index_x_u, :]
                    label_p_pred = model.predict(img_patch.reshape(1, img_patch.shape[0], img_patch.shape[1], img_patch.shape[2]))
                    

                        
                    seg = np.argmax(label_p_pred, axis=3)[0]
                    
                    #print(np.unique(seg),'unitseg')
                    
                    
                    ##if handwrittens:
                        ##boundries_pre = label_p_pred[0,:,:,1]
                        ##boundries_pre[boundries_pre<0.03]=0
                        ##boundries_pre[boundries_pre!=0]=1
                        
                        ###plt.imshow(boundries_pre)
                        ###plt.show()
                        
                        ##seg[boundries_pre==1] = 1
                        
                    seg_color = np.repeat(seg[:, :, np.newaxis], 3, axis=2)

                    if i == 0 and j == 0:
                        seg_color = seg_color[0 : seg_color.shape[0] - margin, 0 : seg_color.shape[1] - margin, :]
                        seg = seg[0 : seg.shape[0] - margin, 0 : seg.shape[1] - margin]
                        mask_true[index_y_d + 0 : index_y_u - margin, index_x_d + 0 : index_x_u - margin] = seg
                        prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + 0 : index_x_u - margin, :] = seg_color
                    elif i == nxf - 1 and j == nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - 0, margin : seg_color.shape[1] - 0, :]
                        seg = seg[margin : seg.shape[0] - 0, margin : seg.shape[1] - 0]
                        mask_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - 0] = seg
                        prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - 0, :] = seg_color
                    elif i == 0 and j == nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - 0, 0 : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - 0, 0 : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - 0, index_x_d + 0 : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + 0 : index_x_u - margin, :] = seg_color
                    elif i == nxf - 1 and j == 0:
                        seg_color = seg_color[0 : seg_color.shape[0] - margin, margin : seg_color.shape[1] - 0, :]
                        seg = seg[0 : seg.shape[0] - margin, margin : seg.shape[1] - 0]
                        mask_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - 0] = seg
                        prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - 0, :] = seg_color
                    elif i == 0 and j != 0 and j != nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - margin, 0 : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - margin, 0 : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - margin, index_x_d + 0 : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + 0 : index_x_u - margin, :] = seg_color
                    elif i == nxf - 1 and j != 0 and j != nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - margin, margin : seg_color.shape[1] - 0, :]
                        seg = seg[margin : seg.shape[0] - margin, margin : seg.shape[1] - 0]
                        mask_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - 0] = seg
                        prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - 0, :] = seg_color
                    elif i != 0 and i != nxf - 1 and j == 0:
                        seg_color = seg_color[0 : seg_color.shape[0] - margin, margin : seg_color.shape[1] - margin, :]
                        seg = seg[0 : seg.shape[0] - margin, margin : seg.shape[1] - margin]
                        mask_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - margin] = seg
                        prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - margin, :] = seg_color
                    elif i != 0 and i != nxf - 1 and j == nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - 0, margin : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - 0, margin : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - margin, :] = seg_color
                    else:
                        seg_color = seg_color[margin : seg_color.shape[0] - margin, margin : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - margin, margin : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - margin, :] = seg_color

            prediction_true = prediction_true.astype(np.uint8)
        del model
        gc.collect()
        return prediction_true
    def do_prediction_new_concept(self, patches, img, model, marginal_of_patch_percent=0.1):
        self.logger.debug("enter do_prediction")

        img_height_model = model.layers[len(model.layers) - 1].output_shape[1]
        img_width_model = model.layers[len(model.layers) - 1].output_shape[2]

        if not patches:
            img_h_page = img.shape[0]
            img_w_page = img.shape[1]
            img = img / float(255.0)
            img = resize_image(img, img_height_model, img_width_model)

            label_p_pred = model.predict(img.reshape(1, img.shape[0], img.shape[1], img.shape[2]))

            seg = np.argmax(label_p_pred, axis=3)[0]
            seg_color = np.repeat(seg[:, :, np.newaxis], 3, axis=2)
            prediction_true = resize_image(seg_color, img_h_page, img_w_page)
            prediction_true = prediction_true.astype(np.uint8)


        else:
            if img.shape[0] < img_height_model:
                img = resize_image(img, img_height_model, img.shape[1])

            if img.shape[1] < img_width_model:
                img = resize_image(img, img.shape[0], img_width_model)

            self.logger.info("Image dimensions: %sx%s", img_height_model, img_width_model)
            margin = int(marginal_of_patch_percent * img_height_model)
            width_mid = img_width_model - 2 * margin
            height_mid = img_height_model - 2 * margin
            img = img / float(255.0)
            img = img.astype(np.float16)
            img_h = img.shape[0]
            img_w = img.shape[1]
            prediction_true = np.zeros((img_h, img_w, 3))
            mask_true = np.zeros((img_h, img_w))
            nxf = img_w / float(width_mid)
            nyf = img_h / float(height_mid)
            nxf = int(nxf) + 1 if nxf > int(nxf) else int(nxf)
            nyf = int(nyf) + 1 if nyf > int(nyf) else int(nyf)

            for i in range(nxf):
                for j in range(nyf):
                    if i == 0:
                        index_x_d = i * width_mid
                        index_x_u = index_x_d + img_width_model
                    else:
                        index_x_d = i * width_mid
                        index_x_u = index_x_d + img_width_model
                    if j == 0:
                        index_y_d = j * height_mid
                        index_y_u = index_y_d + img_height_model
                    else:
                        index_y_d = j * height_mid
                        index_y_u = index_y_d + img_height_model
                    if index_x_u > img_w:
                        index_x_u = img_w
                        index_x_d = img_w - img_width_model
                    if index_y_u > img_h:
                        index_y_u = img_h
                        index_y_d = img_h - img_height_model

                    img_patch = img[index_y_d:index_y_u, index_x_d:index_x_u, :]
                    label_p_pred = model.predict(img_patch.reshape(1, img_patch.shape[0], img_patch.shape[1], img_patch.shape[2]))
                    seg = np.argmax(label_p_pred, axis=3)[0]
                    
                    
                    seg_not_base = label_p_pred[0,:,:,4]
                    ##seg2 = -label_p_pred[0,:,:,2]
                    
                    
                    seg_not_base[seg_not_base>0.03] =1
                    seg_not_base[seg_not_base<1] =0
                    
                    
                    
                    seg_test = label_p_pred[0,:,:,1]
                    ##seg2 = -label_p_pred[0,:,:,2]
                    
                    
                    seg_test[seg_test>0.75] =1
                    seg_test[seg_test<1] =0
                    
                    
                    seg_line = label_p_pred[0,:,:,3]
                    ##seg2 = -label_p_pred[0,:,:,2]
                    
                    
                    seg_line[seg_line>0.1] =1
                    seg_line[seg_line<1] =0
                    
                    
                    seg_background = label_p_pred[0,:,:,0]
                    ##seg2 = -label_p_pred[0,:,:,2]
                    
                    
                    seg_background[seg_background>0.25] =1
                    seg_background[seg_background<1] =0
                    ##seg = seg+seg2
                    #seg = label_p_pred[0,:,:,2]
                    #seg[seg>0.4] =1
                    #seg[seg<1] =0
                    
                    ##plt.imshow(seg_test)
                    ##plt.show()
                    
                    ##plt.imshow(seg_background)
                    ##plt.show()
                    #seg[seg==1]=0
                    #seg[seg_test==1]=1
                    seg[seg_not_base==1]=4
                    seg[seg_background==1]=0
                    seg[(seg_line==1) & (seg==0)]=3
                    seg_color = np.repeat(seg[:, :, np.newaxis], 3, axis=2)

                    if i == 0 and j == 0:
                        seg_color = seg_color[0 : seg_color.shape[0] - margin, 0 : seg_color.shape[1] - margin, :]
                        seg = seg[0 : seg.shape[0] - margin, 0 : seg.shape[1] - margin]
                        mask_true[index_y_d + 0 : index_y_u - margin, index_x_d + 0 : index_x_u - margin] = seg
                        prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + 0 : index_x_u - margin, :] = seg_color
                    elif i == nxf - 1 and j == nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - 0, margin : seg_color.shape[1] - 0, :]
                        seg = seg[margin : seg.shape[0] - 0, margin : seg.shape[1] - 0]
                        mask_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - 0] = seg
                        prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - 0, :] = seg_color
                    elif i == 0 and j == nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - 0, 0 : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - 0, 0 : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - 0, index_x_d + 0 : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + 0 : index_x_u - margin, :] = seg_color
                    elif i == nxf - 1 and j == 0:
                        seg_color = seg_color[0 : seg_color.shape[0] - margin, margin : seg_color.shape[1] - 0, :]
                        seg = seg[0 : seg.shape[0] - margin, margin : seg.shape[1] - 0]
                        mask_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - 0] = seg
                        prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - 0, :] = seg_color
                    elif i == 0 and j != 0 and j != nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - margin, 0 : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - margin, 0 : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - margin, index_x_d + 0 : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + 0 : index_x_u - margin, :] = seg_color
                    elif i == nxf - 1 and j != 0 and j != nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - margin, margin : seg_color.shape[1] - 0, :]
                        seg = seg[margin : seg.shape[0] - margin, margin : seg.shape[1] - 0]
                        mask_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - 0] = seg
                        prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - 0, :] = seg_color
                    elif i != 0 and i != nxf - 1 and j == 0:
                        seg_color = seg_color[0 : seg_color.shape[0] - margin, margin : seg_color.shape[1] - margin, :]
                        seg = seg[0 : seg.shape[0] - margin, margin : seg.shape[1] - margin]
                        mask_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - margin] = seg
                        prediction_true[index_y_d + 0 : index_y_u - margin, index_x_d + margin : index_x_u - margin, :] = seg_color
                    elif i != 0 and i != nxf - 1 and j == nyf - 1:
                        seg_color = seg_color[margin : seg_color.shape[0] - 0, margin : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - 0, margin : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - 0, index_x_d + margin : index_x_u - margin, :] = seg_color
                    else:
                        seg_color = seg_color[margin : seg_color.shape[0] - margin, margin : seg_color.shape[1] - margin, :]
                        seg = seg[margin : seg.shape[0] - margin, margin : seg.shape[1] - margin]
                        mask_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - margin] = seg
                        prediction_true[index_y_d + margin : index_y_u - margin, index_x_d + margin : index_x_u - margin, :] = seg_color

            prediction_true = prediction_true.astype(np.uint8)
        del model
        gc.collect()
        return prediction_true

    def early_page_for_num_of_column_classification(self,img_bin):
        self.logger.debug("enter early_page_for_num_of_column_classification")
        if self.input_binary:
            img =np.copy(img_bin)
            img = img.astype(np.uint8)
        else:
            img = self.imread()
        model_page, session_page = self.start_new_session_and_model(self.model_page_dir)
        img = cv2.GaussianBlur(img, (5, 5), 0)

        img_page_prediction = self.do_prediction(False, img, model_page)

        imgray = cv2.cvtColor(img_page_prediction, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(imgray, 0, 255, 0)
        thresh = cv2.dilate(thresh, KERNEL, iterations=3)
        contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours)>0:
            cnt_size = np.array([cv2.contourArea(contours[j]) for j in range(len(contours))])
            cnt = contours[np.argmax(cnt_size)]
            x, y, w, h = cv2.boundingRect(cnt)
            box = [x, y, w, h]
        else:
            box = [0, 0, img.shape[1], img.shape[0]]
        croped_page, page_coord = crop_image_inside_box(box, img)
        session_page.close()
        del model_page
        del session_page
        gc.collect()
        K.clear_session()
        self.logger.debug("exit early_page_for_num_of_column_classification")
        return croped_page, page_coord

    def extract_page(self):
        self.logger.debug("enter extract_page")
        cont_page = []
        model_page, session_page = self.start_new_session_and_model(self.model_page_dir)
        img = cv2.GaussianBlur(self.image, (5, 5), 0)
        img_page_prediction = self.do_prediction(False, img, model_page)
        imgray = cv2.cvtColor(img_page_prediction, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(imgray, 0, 255, 0)
        thresh = cv2.dilate(thresh, KERNEL, iterations=3)
        contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        
        if len(contours)>0:
            cnt_size = np.array([cv2.contourArea(contours[j]) for j in range(len(contours))])
            cnt = contours[np.argmax(cnt_size)]
            x, y, w, h = cv2.boundingRect(cnt)
            if x <= 30:
                w += x
                x = 0
            if (self.image.shape[1] - (x + w)) <= 30:
                w = w + (self.image.shape[1] - (x + w))
            if y <= 30:
                h = h + y
                y = 0
            if (self.image.shape[0] - (y + h)) <= 30:
                h = h + (self.image.shape[0] - (y + h))

            box = [x, y, w, h]
        else:
            box = [0, 0, img.shape[1], img.shape[0]]
        croped_page, page_coord = crop_image_inside_box(box, self.image)
        cont_page.append(np.array([[page_coord[2], page_coord[0]], [page_coord[3], page_coord[0]], [page_coord[3], page_coord[1]], [page_coord[2], page_coord[1]]]))
        session_page.close()
        del model_page
        del session_page
        gc.collect()
        K.clear_session()
        self.logger.debug("exit extract_page")
        return croped_page, page_coord, cont_page

    def early_page_for_num_of_column_classification(self,img_bin):
        self.logger.debug("enter early_page_for_num_of_column_classification")
        if self.input_binary:
            img =np.copy(img_bin)
            img = img.astype(np.uint8)
        else:
            img = self.imread()
        model_page, session_page = self.start_new_session_and_model(self.model_page_dir)
        img = cv2.GaussianBlur(img, (5, 5), 0)

        img_page_prediction = self.do_prediction(False, img, model_page)

        imgray = cv2.cvtColor(img_page_prediction, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(imgray, 0, 255, 0)
        thresh = cv2.dilate(thresh, KERNEL, iterations=3)
        contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours)>0:
            cnt_size = np.array([cv2.contourArea(contours[j]) for j in range(len(contours))])
            cnt = contours[np.argmax(cnt_size)]
            x, y, w, h = cv2.boundingRect(cnt)
            box = [x, y, w, h]
        else:
            box = [0, 0, img.shape[1], img.shape[0]]
        croped_page, page_coord = crop_image_inside_box(box, img)
        session_page.close()
        del model_page
        del session_page
        gc.collect()
        K.clear_session()
        self.logger.debug("exit early_page_for_num_of_column_classification")
        return croped_page, page_coord

    def extract_page(self):
        self.logger.debug("enter extract_page")
        cont_page = []
        model_page, session_page = self.start_new_session_and_model(self.model_page_dir)
        img = cv2.GaussianBlur(self.image, (5, 5), 0)
        img_page_prediction = self.do_prediction(False, img, model_page)
        imgray = cv2.cvtColor(img_page_prediction, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(imgray, 0, 255, 0)
        thresh = cv2.dilate(thresh, KERNEL, iterations=3)
        contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        
        if len(contours)>0:
            cnt_size = np.array([cv2.contourArea(contours[j]) for j in range(len(contours))])
            cnt = contours[np.argmax(cnt_size)]
            x, y, w, h = cv2.boundingRect(cnt)
            if x <= 30:
                w += x
                x = 0
            if (self.image.shape[1] - (x + w)) <= 30:
                w = w + (self.image.shape[1] - (x + w))
            if y <= 30:
                h = h + y
                y = 0
            if (self.image.shape[0] - (y + h)) <= 30:
                h = h + (self.image.shape[0] - (y + h))

            box = [x, y, w, h]
        else:
            box = [0, 0, img.shape[1], img.shape[0]]
        croped_page, page_coord = crop_image_inside_box(box, self.image)
        cont_page.append(np.array([[page_coord[2], page_coord[0]], [page_coord[3], page_coord[0]], [page_coord[3], page_coord[1]], [page_coord[2], page_coord[1]]]))
        session_page.close()
        del model_page
        del session_page
        gc.collect()
        K.clear_session()
        self.logger.debug("exit extract_page")
        return croped_page, page_coord, cont_page

    def extract_text_regions(self, img, patches, cols):
        self.logger.debug("enter extract_text_regions")
        img_height_h = img.shape[0]
        img_width_h = img.shape[1]

        model_region, session_region = self.start_new_session_and_model(self.model_region_dir_fully if patches else self.model_region_dir_fully_np)

        if not patches:
            img = otsu_copy_binary(img)
            img = img.astype(np.uint8)
            prediction_regions2 = None
        else:
            if cols == 1:
                img2 = otsu_copy_binary(img)
                img2 = img2.astype(np.uint8)
                img2 = resize_image(img2, int(img_height_h * 0.7), int(img_width_h * 0.7))
                marginal_of_patch_percent = 0.1
                prediction_regions2 = self.do_prediction(patches, img2, model_region, marginal_of_patch_percent)
                prediction_regions2 = resize_image(prediction_regions2, img_height_h, img_width_h)

            if cols == 2:
                img2 = otsu_copy_binary(img)
                img2 = img2.astype(np.uint8)
                img2 = resize_image(img2, int(img_height_h * 0.4), int(img_width_h * 0.4))
                marginal_of_patch_percent = 0.1
                prediction_regions2 = self.do_prediction(patches, img2, model_region, marginal_of_patch_percent)
                prediction_regions2 = resize_image(prediction_regions2, img_height_h, img_width_h)

            elif cols > 2:
                img2 = otsu_copy_binary(img)
                img2 = img2.astype(np.uint8)
                img2 = resize_image(img2, int(img_height_h * 0.3), int(img_width_h * 0.3))
                marginal_of_patch_percent = 0.1
                prediction_regions2 = self.do_prediction(patches, img2, model_region, marginal_of_patch_percent)
                prediction_regions2 = resize_image(prediction_regions2, img_height_h, img_width_h)

            if cols == 2:
                img = otsu_copy_binary(img)
                img = img.astype(np.uint8)
                if img_width_h >= 2000:
                    img = resize_image(img, int(img_height_h * 0.9), int(img_width_h * 0.9))
                img = img.astype(np.uint8)

            if cols == 1:
                img = otsu_copy_binary(img)
                img = img.astype(np.uint8)
                img = resize_image(img, int(img_height_h * 0.5), int(img_width_h * 0.5))
                img = img.astype(np.uint8)

            if cols == 3:
                if (self.scale_x == 1 and img_width_h > 3000) or (self.scale_x != 1 and img_width_h > 2800):
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)
                    img = resize_image(img, int(img_height_h * 2800 / float(img_width_h)), 2800)
                else:
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)

            if cols == 4:
                if (self.scale_x == 1 and img_width_h > 4000) or (self.scale_x != 1 and img_width_h > 3700):
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)
                    img= resize_image(img, int(img_height_h * 3700 / float(img_width_h)), 3700)
                else:
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)
                    img= resize_image(img, int(img_height_h * 0.9), int(img_width_h * 0.9))

            if cols == 5:
                if self.scale_x == 1 and img_width_h > 5000:
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)
                    img= resize_image(img, int(img_height_h * 0.7), int(img_width_h * 0.7))
                else:
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)
                    img= resize_image(img, int(img_height_h * 0.9), int(img_width_h * 0.9) )

            if cols >= 6:
                if img_width_h > 5600:
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)
                    img= resize_image(img, int(img_height_h * 5600 / float(img_width_h)), 5600)
                else:
                    img = otsu_copy_binary(img)
                    img = img.astype(np.uint8)
                    img= resize_image(img, int(img_height_h * 0.9), int(img_width_h * 0.9))

        marginal_of_patch_percent = 0.1
        prediction_regions = self.do_prediction(patches, img, model_region, marginal_of_patch_percent)
        prediction_regions = resize_image(prediction_regions, img_height_h, img_width_h)

        session_region.close()
        del model_region
        del session_region
        gc.collect()
        
        self.logger.debug("exit extract_text_regions")
        return prediction_regions, prediction_regions2
    
    def get_slopes_and_deskew_new_light(self, contours, contours_par, textline_mask_tot, image_page_rotated, boxes, slope_deskew):
        self.logger.debug("enter get_slopes_and_deskew_new")
        num_cores = cpu_count()
        queue_of_all_params = Queue()

        processes = []
        nh = np.linspace(0, len(boxes), num_cores + 1)
        indexes_by_text_con = np.array(range(len(contours_par)))
        for i in range(num_cores):
            boxes_per_process = boxes[int(nh[i]) : int(nh[i + 1])]
            contours_per_process = contours[int(nh[i]) : int(nh[i + 1])]
            contours_par_per_process = contours_par[int(nh[i]) : int(nh[i + 1])]
            indexes_text_con_per_process = indexes_by_text_con[int(nh[i]) : int(nh[i + 1])]

            processes.append(Process(target=self.do_work_of_slopes_new_light, args=(queue_of_all_params, boxes_per_process, textline_mask_tot, contours_per_process, contours_par_per_process, indexes_text_con_per_process, image_page_rotated, slope_deskew)))
        for i in range(num_cores):
            processes[i].start()

        slopes = []
        all_found_texline_polygons = []
        all_found_text_regions = []
        all_found_text_regions_par = []
        boxes = []
        all_box_coord = []
        all_index_text_con = []
        for i in range(num_cores):
            list_all_par = queue_of_all_params.get(True)
            slopes_for_sub_process = list_all_par[0]
            polys_for_sub_process = list_all_par[1]
            boxes_for_sub_process = list_all_par[2]
            contours_for_subprocess = list_all_par[3]
            contours_par_for_subprocess = list_all_par[4]
            boxes_coord_for_subprocess = list_all_par[5]
            indexes_for_subprocess = list_all_par[6]
            for j in range(len(slopes_for_sub_process)):
                slopes.append(slopes_for_sub_process[j])
                all_found_texline_polygons.append(polys_for_sub_process[j])
                boxes.append(boxes_for_sub_process[j])
                all_found_text_regions.append(contours_for_subprocess[j])
                all_found_text_regions_par.append(contours_par_for_subprocess[j])
                all_box_coord.append(boxes_coord_for_subprocess[j])
                all_index_text_con.append(indexes_for_subprocess[j])
        for i in range(num_cores):
            processes[i].join()
        self.logger.debug('slopes %s', slopes)
        self.logger.debug("exit get_slopes_and_deskew_new")
        return slopes, all_found_texline_polygons, boxes, all_found_text_regions, all_found_text_regions_par, all_box_coord, all_index_text_con

    def get_slopes_and_deskew_new(self, contours, contours_par, textline_mask_tot, image_page_rotated, boxes, slope_deskew):
        self.logger.debug("enter get_slopes_and_deskew_new")
        num_cores = cpu_count()
        queue_of_all_params = Queue()

        processes = []
        nh = np.linspace(0, len(boxes), num_cores + 1)
        indexes_by_text_con = np.array(range(len(contours_par)))
        for i in range(num_cores):
            boxes_per_process = boxes[int(nh[i]) : int(nh[i + 1])]
            contours_per_process = contours[int(nh[i]) : int(nh[i + 1])]
            contours_par_per_process = contours_par[int(nh[i]) : int(nh[i + 1])]
            indexes_text_con_per_process = indexes_by_text_con[int(nh[i]) : int(nh[i + 1])]

            processes.append(Process(target=self.do_work_of_slopes_new, args=(queue_of_all_params, boxes_per_process, textline_mask_tot, contours_per_process, contours_par_per_process, indexes_text_con_per_process, image_page_rotated, slope_deskew)))
        for i in range(num_cores):
            processes[i].start()

        slopes = []
        all_found_texline_polygons = []
        all_found_text_regions = []
        all_found_text_regions_par = []
        boxes = []
        all_box_coord = []
        all_index_text_con = []
        for i in range(num_cores):
            list_all_par = queue_of_all_params.get(True)
            slopes_for_sub_process = list_all_par[0]
            polys_for_sub_process = list_all_par[1]
            boxes_for_sub_process = list_all_par[2]
            contours_for_subprocess = list_all_par[3]
            contours_par_for_subprocess = list_all_par[4]
            boxes_coord_for_subprocess = list_all_par[5]
            indexes_for_subprocess = list_all_par[6]
            for j in range(len(slopes_for_sub_process)):
                slopes.append(slopes_for_sub_process[j])
                all_found_texline_polygons.append(polys_for_sub_process[j])
                boxes.append(boxes_for_sub_process[j])
                all_found_text_regions.append(contours_for_subprocess[j])
                all_found_text_regions_par.append(contours_par_for_subprocess[j])
                all_box_coord.append(boxes_coord_for_subprocess[j])
                all_index_text_con.append(indexes_for_subprocess[j])
        for i in range(num_cores):
            processes[i].join()
        self.logger.debug('slopes %s', slopes)
        self.logger.debug("exit get_slopes_and_deskew_new")
        return slopes, all_found_texline_polygons, boxes, all_found_text_regions, all_found_text_regions_par, all_box_coord, all_index_text_con

    def get_slopes_and_deskew_new_curved(self, contours, contours_par, textline_mask_tot, image_page_rotated, boxes, mask_texts_only, num_col, scale_par, slope_deskew):
        self.logger.debug("enter get_slopes_and_deskew_new_curved")
        num_cores = cpu_count()
        queue_of_all_params = Queue()

        processes = []
        nh = np.linspace(0, len(boxes), num_cores + 1)
        indexes_by_text_con = np.array(range(len(contours_par)))

        for i in range(num_cores):
            boxes_per_process = boxes[int(nh[i]) : int(nh[i + 1])]
            contours_per_process = contours[int(nh[i]) : int(nh[i + 1])]
            contours_par_per_process = contours_par[int(nh[i]) : int(nh[i + 1])]
            indexes_text_con_per_process = indexes_by_text_con[int(nh[i]) : int(nh[i + 1])]

            processes.append(Process(target=self.do_work_of_slopes_new_curved, args=(queue_of_all_params, boxes_per_process, textline_mask_tot, contours_per_process, contours_par_per_process, image_page_rotated, mask_texts_only, num_col, scale_par, indexes_text_con_per_process, slope_deskew)))

        for i in range(num_cores):
            processes[i].start()

        slopes = []
        all_found_texline_polygons = []
        all_found_text_regions = []
        all_found_text_regions_par = []
        boxes = []
        all_box_coord = []
        all_index_text_con = []

        for i in range(num_cores):
            list_all_par = queue_of_all_params.get(True)
            polys_for_sub_process = list_all_par[0]
            boxes_for_sub_process = list_all_par[1]
            contours_for_subprocess = list_all_par[2]
            contours_par_for_subprocess = list_all_par[3]
            boxes_coord_for_subprocess = list_all_par[4]
            indexes_for_subprocess = list_all_par[5]
            slopes_for_sub_process = list_all_par[6]
            for j in range(len(polys_for_sub_process)):
                slopes.append(slopes_for_sub_process[j])
                all_found_texline_polygons.append(polys_for_sub_process[j][::-1])
                boxes.append(boxes_for_sub_process[j])
                all_found_text_regions.append(contours_for_subprocess[j])
                all_found_text_regions_par.append(contours_par_for_subprocess[j])
                all_box_coord.append(boxes_coord_for_subprocess[j])
                all_index_text_con.append(indexes_for_subprocess[j])

        for i in range(num_cores):
            processes[i].join()
        # print(slopes,'slopes')
        return all_found_texline_polygons, boxes, all_found_text_regions, all_found_text_regions_par, all_box_coord, all_index_text_con, slopes

    def do_work_of_slopes_new_curved(self, queue_of_all_params, boxes_text, textline_mask_tot_ea, contours_per_process, contours_par_per_process, image_page_rotated, mask_texts_only, num_col, scale_par, indexes_r_con_per_pro, slope_deskew):
        self.logger.debug("enter do_work_of_slopes_new_curved")
        slopes_per_each_subprocess = []
        bounding_box_of_textregion_per_each_subprocess = []
        textlines_rectangles_per_each_subprocess = []
        contours_textregion_per_each_subprocess = []
        contours_textregion_par_per_each_subprocess = []
        all_box_coord_per_process = []
        index_by_text_region_contours = []

        textline_cnt_separated = np.zeros(textline_mask_tot_ea.shape)

        for mv in range(len(boxes_text)):

            all_text_region_raw = textline_mask_tot_ea[boxes_text[mv][1] : boxes_text[mv][1] + boxes_text[mv][3], boxes_text[mv][0] : boxes_text[mv][0] + boxes_text[mv][2]]
            all_text_region_raw = all_text_region_raw.astype(np.uint8)
            img_int_p = all_text_region_raw[:, :]

            # img_int_p=cv2.erode(img_int_p,KERNEL,iterations = 2)
            # plt.imshow(img_int_p)
            # plt.show()

            if img_int_p.shape[0] / img_int_p.shape[1] < 0.1:
                slopes_per_each_subprocess.append(0)
                slope_for_all = [slope_deskew][0]
            else:
                try:
                    textline_con, hierarchy = return_contours_of_image(img_int_p)
                    textline_con_fil = filter_contours_area_of_image(img_int_p, textline_con, hierarchy, max_area=1, min_area=0.0008)
                    y_diff_mean = find_contours_mean_y_diff(textline_con_fil)
                    if self.isNaN(y_diff_mean):
                        slope_for_all = MAX_SLOPE
                    else:
                        sigma_des = max(1, int(y_diff_mean * (4.0 / 40.0)))
                        img_int_p[img_int_p > 0] = 1
                        slope_for_all = return_deskew_slop(img_int_p, sigma_des, plotter=self.plotter)

                        if abs(slope_for_all) < 0.5:
                            slope_for_all = [slope_deskew][0]

                except Exception as why:
                    self.logger.error(why)
                    slope_for_all = MAX_SLOPE

                if slope_for_all == MAX_SLOPE:
                    slope_for_all = [slope_deskew][0]
                slopes_per_each_subprocess.append(slope_for_all)

            index_by_text_region_contours.append(indexes_r_con_per_pro[mv])
            _, crop_coor = crop_image_inside_box(boxes_text[mv], image_page_rotated)

            if abs(slope_for_all) < 45:
                # all_box_coord.append(crop_coor)
                textline_region_in_image = np.zeros(textline_mask_tot_ea.shape)
                cnt_o_t_max = contours_par_per_process[mv]
                x, y, w, h = cv2.boundingRect(cnt_o_t_max)
                mask_biggest = np.zeros(mask_texts_only.shape)
                mask_biggest = cv2.fillPoly(mask_biggest, pts=[cnt_o_t_max], color=(1, 1, 1))
                mask_region_in_patch_region = mask_biggest[y : y + h, x : x + w]
                textline_biggest_region = mask_biggest * textline_mask_tot_ea

                # print(slope_for_all,'slope_for_all')
                textline_rotated_separated = separate_lines_new2(textline_biggest_region[y : y + h, x : x + w], 0, num_col, slope_for_all, plotter=self.plotter)

                # new line added
                ##print(np.shape(textline_rotated_separated),np.shape(mask_biggest))
                textline_rotated_separated[mask_region_in_patch_region[:, :] != 1] = 0
                # till here

                textline_cnt_separated[y : y + h, x : x + w] = textline_rotated_separated
                textline_region_in_image[y : y + h, x : x + w] = textline_rotated_separated

                # plt.imshow(textline_region_in_image)
                # plt.show()
                # plt.imshow(textline_cnt_separated)
                # plt.show()

                pixel_img = 1
                cnt_textlines_in_image = return_contours_of_interested_textline(textline_region_in_image, pixel_img)

                textlines_cnt_per_region = []
                for jjjj in range(len(cnt_textlines_in_image)):
                    mask_biggest2 = np.zeros(mask_texts_only.shape)
                    mask_biggest2 = cv2.fillPoly(mask_biggest2, pts=[cnt_textlines_in_image[jjjj]], color=(1, 1, 1))
                    if num_col + 1 == 1:
                        mask_biggest2 = cv2.dilate(mask_biggest2, KERNEL, iterations=5)
                    else:
                        mask_biggest2 = cv2.dilate(mask_biggest2, KERNEL, iterations=4)

                    pixel_img = 1
                    mask_biggest2 = resize_image(mask_biggest2, int(mask_biggest2.shape[0] * scale_par), int(mask_biggest2.shape[1] * scale_par))
                    cnt_textlines_in_image_ind = return_contours_of_interested_textline(mask_biggest2, pixel_img)
                    try:
                        textlines_cnt_per_region.append(cnt_textlines_in_image_ind[0])
                    except Exception as why:
                        self.logger.error(why)
            else:
                add_boxes_coor_into_textlines = True
                textlines_cnt_per_region = textline_contours_postprocessing(all_text_region_raw, slope_for_all, contours_par_per_process[mv], boxes_text[mv], add_boxes_coor_into_textlines)
                add_boxes_coor_into_textlines = False
                # print(np.shape(textlines_cnt_per_region),'textlines_cnt_per_region')

            textlines_rectangles_per_each_subprocess.append(textlines_cnt_per_region)
            bounding_box_of_textregion_per_each_subprocess.append(boxes_text[mv])
            contours_textregion_per_each_subprocess.append(contours_per_process[mv])
            contours_textregion_par_per_each_subprocess.append(contours_par_per_process[mv])
            all_box_coord_per_process.append(crop_coor)

        queue_of_all_params.put([textlines_rectangles_per_each_subprocess, bounding_box_of_textregion_per_each_subprocess, contours_textregion_per_each_subprocess, contours_textregion_par_per_each_subprocess, all_box_coord_per_process, index_by_text_region_contours, slopes_per_each_subprocess])
    def do_work_of_slopes_new_light(self, queue_of_all_params, boxes_text, textline_mask_tot_ea, contours_per_process, contours_par_per_process, indexes_r_con_per_pro, image_page_rotated, slope_deskew):
        self.logger.debug('enter do_work_of_slopes_new')
        slopes_per_each_subprocess = []
        bounding_box_of_textregion_per_each_subprocess = []
        textlines_rectangles_per_each_subprocess = []
        contours_textregion_per_each_subprocess = []
        contours_textregion_par_per_each_subprocess = []
        all_box_coord_per_process = []
        index_by_text_region_contours = []
        for mv in range(len(boxes_text)):
            _, crop_coor = crop_image_inside_box(boxes_text[mv],image_page_rotated)
            mask_textline = np.zeros((textline_mask_tot_ea.shape))
            mask_textline = cv2.fillPoly(mask_textline,pts=[contours_per_process[mv]],color=(1,1,1))
            all_text_region_raw = (textline_mask_tot_ea*mask_textline[:,:])[boxes_text[mv][1]:boxes_text[mv][1]+boxes_text[mv][3] , boxes_text[mv][0]:boxes_text[mv][0]+boxes_text[mv][2] ]
            all_text_region_raw=all_text_region_raw.astype(np.uint8)

            slopes_per_each_subprocess.append([slope_deskew][0])
            mask_only_con_region = np.zeros(textline_mask_tot_ea.shape)
            mask_only_con_region = cv2.fillPoly(mask_only_con_region, pts=[contours_par_per_process[mv]], color=(1, 1, 1))

            # plt.imshow(mask_only_con_region)
            # plt.show()
            all_text_region_raw = np.copy(textline_mask_tot_ea[boxes_text[mv][1] : boxes_text[mv][1] + boxes_text[mv][3], boxes_text[mv][0] : boxes_text[mv][0] + boxes_text[mv][2]])
            mask_only_con_region = mask_only_con_region[boxes_text[mv][1] : boxes_text[mv][1] + boxes_text[mv][3], boxes_text[mv][0] : boxes_text[mv][0] + boxes_text[mv][2]]


            all_text_region_raw[mask_only_con_region == 0] = 0
            cnt_clean_rot = textline_contours_postprocessing(all_text_region_raw, [slope_deskew][0], contours_par_per_process[mv], boxes_text[mv])

            textlines_rectangles_per_each_subprocess.append(cnt_clean_rot)
            index_by_text_region_contours.append(indexes_r_con_per_pro[mv])
            bounding_box_of_textregion_per_each_subprocess.append(boxes_text[mv])

            contours_textregion_per_each_subprocess.append(contours_per_process[mv])
            contours_textregion_par_per_each_subprocess.append(contours_par_per_process[mv])
            all_box_coord_per_process.append(crop_coor)
        queue_of_all_params.put([slopes_per_each_subprocess, textlines_rectangles_per_each_subprocess, bounding_box_of_textregion_per_each_subprocess, contours_textregion_per_each_subprocess, contours_textregion_par_per_each_subprocess, all_box_coord_per_process, index_by_text_region_contours])
        
    def do_work_of_slopes_new(self, queue_of_all_params, boxes_text, textline_mask_tot_ea, contours_per_process, contours_par_per_process, indexes_r_con_per_pro, image_page_rotated, slope_deskew):
        self.logger.debug('enter do_work_of_slopes_new')
        slopes_per_each_subprocess = []
        bounding_box_of_textregion_per_each_subprocess = []
        textlines_rectangles_per_each_subprocess = []
        contours_textregion_per_each_subprocess = []
        contours_textregion_par_per_each_subprocess = []
        all_box_coord_per_process = []
        index_by_text_region_contours = []
        for mv in range(len(boxes_text)):
            _, crop_coor = crop_image_inside_box(boxes_text[mv],image_page_rotated)
            mask_textline = np.zeros((textline_mask_tot_ea.shape))
            mask_textline = cv2.fillPoly(mask_textline,pts=[contours_per_process[mv]],color=(1,1,1))
            all_text_region_raw = (textline_mask_tot_ea*mask_textline[:,:])[boxes_text[mv][1]:boxes_text[mv][1]+boxes_text[mv][3] , boxes_text[mv][0]:boxes_text[mv][0]+boxes_text[mv][2] ]
            all_text_region_raw=all_text_region_raw.astype(np.uint8)
            img_int_p=all_text_region_raw[:,:]#self.all_text_region_raw[mv]
            img_int_p=cv2.erode(img_int_p,KERNEL,iterations = 2)

            if img_int_p.shape[0]/img_int_p.shape[1]<0.1:
                slopes_per_each_subprocess.append(0)
                slope_for_all = [slope_deskew][0]
                all_text_region_raw = textline_mask_tot_ea[boxes_text[mv][1] : boxes_text[mv][1] + boxes_text[mv][3], boxes_text[mv][0] : boxes_text[mv][0] + boxes_text[mv][2]]
                cnt_clean_rot = textline_contours_postprocessing(all_text_region_raw, slope_for_all, contours_par_per_process[mv], boxes_text[mv], 0)
                textlines_rectangles_per_each_subprocess.append(cnt_clean_rot)
                index_by_text_region_contours.append(indexes_r_con_per_pro[mv])
                bounding_box_of_textregion_per_each_subprocess.append(boxes_text[mv])
            else:
                try:
                    textline_con, hierarchy = return_contours_of_image(img_int_p)
                    textline_con_fil = filter_contours_area_of_image(img_int_p, textline_con, hierarchy, max_area=1, min_area=0.00008)
                    y_diff_mean = find_contours_mean_y_diff(textline_con_fil)
                    if self.isNaN(y_diff_mean):
                        slope_for_all = MAX_SLOPE
                    else:
                        sigma_des = int(y_diff_mean * (4.0 / 40.0))
                        if sigma_des < 1:
                            sigma_des = 1
                        img_int_p[img_int_p > 0] = 1
                        slope_for_all = return_deskew_slop(img_int_p, sigma_des, plotter=self.plotter)
                        if abs(slope_for_all) <= 0.5:
                            slope_for_all = [slope_deskew][0]
                except Exception as why:
                    self.logger.error(why)
                    slope_for_all = MAX_SLOPE
                if slope_for_all == MAX_SLOPE:
                    slope_for_all = [slope_deskew][0]
                slopes_per_each_subprocess.append(slope_for_all)
                mask_only_con_region = np.zeros(textline_mask_tot_ea.shape)
                mask_only_con_region = cv2.fillPoly(mask_only_con_region, pts=[contours_par_per_process[mv]], color=(1, 1, 1))

                # plt.imshow(mask_only_con_region)
                # plt.show()
                all_text_region_raw = np.copy(textline_mask_tot_ea[boxes_text[mv][1] : boxes_text[mv][1] + boxes_text[mv][3], boxes_text[mv][0] : boxes_text[mv][0] + boxes_text[mv][2]])
                mask_only_con_region = mask_only_con_region[boxes_text[mv][1] : boxes_text[mv][1] + boxes_text[mv][3], boxes_text[mv][0] : boxes_text[mv][0] + boxes_text[mv][2]]

                ##plt.imshow(textline_mask_tot_ea)
                ##plt.show()
                ##plt.imshow(all_text_region_raw)
                ##plt.show()
                ##plt.imshow(mask_only_con_region)
                ##plt.show()

                all_text_region_raw[mask_only_con_region == 0] = 0
                cnt_clean_rot = textline_contours_postprocessing(all_text_region_raw, slope_for_all, contours_par_per_process[mv], boxes_text[mv])

                textlines_rectangles_per_each_subprocess.append(cnt_clean_rot)
                index_by_text_region_contours.append(indexes_r_con_per_pro[mv])
                bounding_box_of_textregion_per_each_subprocess.append(boxes_text[mv])

            contours_textregion_per_each_subprocess.append(contours_per_process[mv])
            contours_textregion_par_per_each_subprocess.append(contours_par_per_process[mv])
            all_box_coord_per_process.append(crop_coor)
        queue_of_all_params.put([slopes_per_each_subprocess, textlines_rectangles_per_each_subprocess, bounding_box_of_textregion_per_each_subprocess, contours_textregion_per_each_subprocess, contours_textregion_par_per_each_subprocess, all_box_coord_per_process, index_by_text_region_contours])

    def textline_contours(self, img, patches, scaler_h, scaler_w):
        self.logger.debug('enter textline_contours')

        model_textline, session_textline = self.start_new_session_and_model(self.model_textline_dir if patches else self.model_textline_dir_np)
        img = img.astype(np.uint8)
        img_org = np.copy(img)
        img_h = img_org.shape[0]
        img_w = img_org.shape[1]
        img = resize_image(img_org, int(img_org.shape[0] * scaler_h), int(img_org.shape[1] * scaler_w))
        prediction_textline = self.do_prediction(patches, img, model_textline)
        prediction_textline = resize_image(prediction_textline, img_h, img_w)
        prediction_textline_longshot = self.do_prediction(False, img, model_textline)
        prediction_textline_longshot_true_size = resize_image(prediction_textline_longshot, img_h, img_w)

        session_textline.close()


        return prediction_textline[:, :, 0], prediction_textline_longshot_true_size[:, :, 0]

    def do_work_of_slopes(self, q, poly, box_sub, boxes_per_process, textline_mask_tot, contours_per_process):
        self.logger.debug('enter do_work_of_slopes')
        slope_biggest = 0
        slopes_sub = []
        boxes_sub_new = []
        poly_sub = []
        for mv in range(len(boxes_per_process)):
            crop_img, _ = crop_image_inside_box(boxes_per_process[mv], np.repeat(textline_mask_tot[:, :, np.newaxis], 3, axis=2))
            crop_img = crop_img[:, :, 0]
            crop_img = cv2.erode(crop_img, KERNEL, iterations=2)
            try:
                textline_con, hierarchy = return_contours_of_image(crop_img)
                textline_con_fil = filter_contours_area_of_image(crop_img, textline_con, hierarchy, max_area=1, min_area=0.0008)
                y_diff_mean = find_contours_mean_y_diff(textline_con_fil)
                sigma_des = max(1, int(y_diff_mean * (4.0 / 40.0)))
                crop_img[crop_img > 0] = 1
                slope_corresponding_textregion = return_deskew_slop(crop_img, sigma_des, plotter=self.plotter)
            except Exception as why:
                self.logger.error(why)
                slope_corresponding_textregion = MAX_SLOPE

            if slope_corresponding_textregion == MAX_SLOPE:
                slope_corresponding_textregion = slope_biggest
            slopes_sub.append(slope_corresponding_textregion)

            cnt_clean_rot = textline_contours_postprocessing(crop_img, slope_corresponding_textregion, contours_per_process[mv], boxes_per_process[mv])

            poly_sub.append(cnt_clean_rot)
            boxes_sub_new.append(boxes_per_process[mv])

        q.put(slopes_sub)
        poly.put(poly_sub)
        box_sub.put(boxes_sub_new)
    def get_regions_from_xy_2models_light(self,img,is_image_enhanced, num_col_classifier):
        self.logger.debug("enter get_regions_from_xy_2models")
        erosion_hurts = False
        img_org = np.copy(img)
        img_height_h = img_org.shape[0]
        img_width_h = img_org.shape[1]

        #model_region, session_region = self.start_new_session_and_model(self.model_region_dir_p_ens)

        
        
        if num_col_classifier == 1:
            img_w_new = 1000
            img_h_new = int(img_org.shape[0] / float(img_org.shape[1]) * img_w_new)
            
        elif num_col_classifier == 2:
            img_w_new = 1500
            img_h_new = int(img_org.shape[0] / float(img_org.shape[1]) * img_w_new)
            
        elif num_col_classifier == 3:
            img_w_new = 2000
            img_h_new = int(img_org.shape[0] / float(img_org.shape[1]) * img_w_new)
            
        elif num_col_classifier == 4:
            img_w_new = 2500
            img_h_new = int(img_org.shape[0] / float(img_org.shape[1]) * img_w_new)
        elif num_col_classifier == 5:
            img_w_new = 3000
            img_h_new = int(img_org.shape[0] / float(img_org.shape[1]) * img_w_new)
        else:
            img_w_new = 4000
            img_h_new = int(img_org.shape[0] / float(img_org.shape[1]) * img_w_new)
        gc.collect()
        ##img_resized = resize_image(img_bin,img_height_h, img_width_h )
        img_resized = resize_image(img,img_h_new, img_w_new )
        
        tbin = time.time()
        model_bin, session_bin = self.start_new_session_and_model(self.model_dir_of_binarization)
        print("time bin session", time.time()-tbin)
        prediction_bin = self.do_prediction(True, img_resized, model_bin)
        print("time bin all ", time.time()-tbin)
        prediction_bin=prediction_bin[:,:,0]
        prediction_bin = (prediction_bin[:,:]==0)*1
        prediction_bin = prediction_bin*255
        
        prediction_bin =np.repeat(prediction_bin[:, :, np.newaxis], 3, axis=2)

        session_bin.close()
        del model_bin
        del session_bin
        gc.collect()
        
        prediction_bin = prediction_bin.astype(np.uint16)
        #img= np.copy(prediction_bin)
        img_bin = np.copy(prediction_bin)
        
        
        

        tline = time.time()
        textline_mask_tot_ea = self.run_textline(img_bin)
        print("time line all ", time.time()-tline)
        model_region, session_region = self.start_new_session_and_model(self.model_region_dir_p_ens_light)
        
        
        #plt.imshow(img_bin)
        #plt.show()
        
        prediction_regions_org = self.do_prediction_new_concept(True, img_bin, model_region)
        
        #plt.imshow(prediction_regions_org[:,:,0])
        #plt.show()
            
        prediction_regions_org = resize_image(prediction_regions_org,img_height_h, img_width_h )
        textline_mask_tot_ea = resize_image(textline_mask_tot_ea,img_height_h, img_width_h )
        
        prediction_regions_org=prediction_regions_org[:,:,0]
            
        mask_lines_only = (prediction_regions_org[:,:] ==3)*1
        
        mask_texts_only = (prediction_regions_org[:,:] ==1)*1
        
        mask_images_only=(prediction_regions_org[:,:] ==2)*1
        
        polygons_lines_xml, hir_lines_xml = return_contours_of_image(mask_lines_only)
        polygons_lines_xml = textline_con_fil = filter_contours_area_of_image(mask_lines_only, polygons_lines_xml, hir_lines_xml, max_area=1, min_area=0.00001)
        
        
        polygons_of_only_texts = return_contours_of_interested_region(mask_texts_only,1,0.00001)
        
        polygons_of_only_lines = return_contours_of_interested_region(mask_lines_only,1,0.00001)
        
        
        text_regions_p_true = np.zeros(prediction_regions_org.shape)
        
        text_regions_p_true = cv2.fillPoly(text_regions_p_true, pts = polygons_of_only_lines, color=(3,3,3))
        
        text_regions_p_true[:,:][mask_images_only[:,:] == 1] = 2
        
        text_regions_p_true = cv2.fillPoly(text_regions_p_true, pts = polygons_of_only_texts, color=(1,1,1))
        
        #erosion_hurts = True
        K.clear_session()
        return text_regions_p_true, erosion_hurts, polygons_lines_xml, textline_mask_tot_ea

    def get_regions_from_xy_2models(self,img,is_image_enhanced, num_col_classifier):
        self.logger.debug("enter get_regions_from_xy_2models")
        erosion_hurts = False
        img_org = np.copy(img)
        img_height_h = img_org.shape[0]
        img_width_h = img_org.shape[1]

        model_region, session_region = self.start_new_session_and_model(self.model_region_dir_p_ens)

        ratio_y=1.3
        ratio_x=1

        img = resize_image(img_org, int(img_org.shape[0]*ratio_y), int(img_org.shape[1]*ratio_x))

        prediction_regions_org_y = self.do_prediction(True, img, model_region)
        prediction_regions_org_y = resize_image(prediction_regions_org_y, img_height_h, img_width_h )

        #plt.imshow(prediction_regions_org_y[:,:,0])
        #plt.show()
        prediction_regions_org_y = prediction_regions_org_y[:,:,0]
        mask_zeros_y = (prediction_regions_org_y[:,:]==0)*1
        
        ##img_only_regions_with_sep = ( (prediction_regions_org_y[:,:] != 3) & (prediction_regions_org_y[:,:] != 0) )*1
        img_only_regions_with_sep = ( prediction_regions_org_y[:,:] == 1 )*1
        img_only_regions_with_sep = img_only_regions_with_sep.astype(np.uint8)
        
        try:
            img_only_regions = cv2.erode(img_only_regions_with_sep[:,:], KERNEL, iterations=20)

            _, _ = find_num_col(img_only_regions, num_col_classifier, self.tables, multiplier=6.0)
            
            img = resize_image(img_org, int(img_org.shape[0]), int(img_org.shape[1]*(1.2 if is_image_enhanced else 1)))

            prediction_regions_org = self.do_prediction(True, img, model_region)
            prediction_regions_org = resize_image(prediction_regions_org, img_height_h, img_width_h )

            ##plt.imshow(prediction_regions_org[:,:,0])
            ##plt.show()
            prediction_regions_org=prediction_regions_org[:,:,0]
            prediction_regions_org[(prediction_regions_org[:,:]==1) & (mask_zeros_y[:,:]==1)]=0
            
            session_region.close()
            del model_region
            del session_region
            gc.collect()

            model_region, session_region = self.start_new_session_and_model(self.model_region_dir_p2)
            img = resize_image(img_org, int(img_org.shape[0]), int(img_org.shape[1]))
            prediction_regions_org2 = self.do_prediction(True, img, model_region, 0.2)
            prediction_regions_org2=resize_image(prediction_regions_org2, img_height_h, img_width_h )


            session_region.close()
            del model_region
            del session_region
            gc.collect()

            mask_zeros2 = (prediction_regions_org2[:,:,0] == 0)
            mask_lines2 = (prediction_regions_org2[:,:,0] == 3)
            text_sume_early = (prediction_regions_org[:,:] == 1).sum()
            prediction_regions_org_copy = np.copy(prediction_regions_org)
            prediction_regions_org_copy[(prediction_regions_org_copy[:,:]==1) & (mask_zeros2[:,:]==1)] = 0
            text_sume_second = ((prediction_regions_org_copy[:,:]==1)*1).sum()

            rate_two_models = text_sume_second / float(text_sume_early) * 100

            self.logger.info("ratio_of_two_models: %s", rate_two_models)
            if not(is_image_enhanced and rate_two_models < RATIO_OF_TWO_MODEL_THRESHOLD):
                prediction_regions_org = np.copy(prediction_regions_org_copy)
                
            

            prediction_regions_org[(mask_lines2[:,:]==1) & (prediction_regions_org[:,:]==0)]=3
            mask_lines_only=(prediction_regions_org[:,:]==3)*1
            prediction_regions_org = cv2.erode(prediction_regions_org[:,:], KERNEL, iterations=2)

            #plt.imshow(text_region2_1st_channel)
            #plt.show()

            prediction_regions_org = cv2.dilate(prediction_regions_org[:,:], KERNEL, iterations=2)
            
            
            if rate_two_models<=40:
                if self.input_binary:
                    prediction_bin = np.copy(img_org)
                else:
                    model_bin, session_bin = self.start_new_session_and_model(self.model_dir_of_binarization)
                    prediction_bin = self.do_prediction(True, img_org, model_bin)
                    prediction_bin = resize_image(prediction_bin, img_height_h, img_width_h )
                    
                    prediction_bin=prediction_bin[:,:,0]
                    prediction_bin = (prediction_bin[:,:]==0)*1
                    prediction_bin = prediction_bin*255
                    
                    prediction_bin =np.repeat(prediction_bin[:, :, np.newaxis], 3, axis=2)

                    session_bin.close()
                    del model_bin
                    del session_bin
                    gc.collect()
                
                
                
                model_region, session_region = self.start_new_session_and_model(self.model_region_dir_p_ens)
                ratio_y=1
                ratio_x=1


                img = resize_image(prediction_bin, int(img_org.shape[0]*ratio_y), int(img_org.shape[1]*ratio_x))

                prediction_regions_org = self.do_prediction(True, img, model_region)
                prediction_regions_org = resize_image(prediction_regions_org, img_height_h, img_width_h )
                prediction_regions_org=prediction_regions_org[:,:,0]
                
                mask_lines_only=(prediction_regions_org[:,:]==3)*1
                session_region.close()
                del model_region
                del session_region
                gc.collect()
                
                
            mask_texts_only=(prediction_regions_org[:,:]==1)*1
            mask_images_only=(prediction_regions_org[:,:]==2)*1
            
            
            
            polygons_lines_xml, hir_lines_xml = return_contours_of_image(mask_lines_only)
            polygons_lines_xml = textline_con_fil = filter_contours_area_of_image(mask_lines_only, polygons_lines_xml, hir_lines_xml, max_area=1, min_area=0.00001)

            polygons_of_only_texts = return_contours_of_interested_region(mask_texts_only, 1, 0.00001)
            polygons_of_only_lines = return_contours_of_interested_region(mask_lines_only, 1, 0.00001)

            text_regions_p_true = np.zeros(prediction_regions_org.shape)
            text_regions_p_true = cv2.fillPoly(text_regions_p_true,pts = polygons_of_only_lines, color=(3, 3, 3))
            text_regions_p_true[:,:][mask_images_only[:,:] == 1] = 2

            text_regions_p_true=cv2.fillPoly(text_regions_p_true,pts=polygons_of_only_texts, color=(1,1,1))

            

            K.clear_session()
            return text_regions_p_true, erosion_hurts, polygons_lines_xml
        except:
            
            if self.input_binary:
                prediction_bin = np.copy(img_org)
            else:
                session_region.close()
                del model_region
                del session_region
                gc.collect()
                
                model_bin, session_bin = self.start_new_session_and_model(self.model_dir_of_binarization)
                prediction_bin = self.do_prediction(True, img_org, model_bin)
                prediction_bin = resize_image(prediction_bin, img_height_h, img_width_h )
                prediction_bin=prediction_bin[:,:,0]
                
                prediction_bin = (prediction_bin[:,:]==0)*1
                
                prediction_bin = prediction_bin*255
                
                prediction_bin =np.repeat(prediction_bin[:, :, np.newaxis], 3, axis=2)

                
                
                session_bin.close()
                del model_bin
                del session_bin
                gc.collect()
            
            
            
                model_region, session_region = self.start_new_session_and_model(self.model_region_dir_p_ens)
            ratio_y=1
            ratio_x=1


            img = resize_image(prediction_bin, int(img_org.shape[0]*ratio_y), int(img_org.shape[1]*ratio_x))

            prediction_regions_org = self.do_prediction(True, img, model_region)
            prediction_regions_org = resize_image(prediction_regions_org, img_height_h, img_width_h )
            prediction_regions_org=prediction_regions_org[:,:,0]
            
            #mask_lines_only=(prediction_regions_org[:,:]==3)*1
            session_region.close()
            del model_region
            del session_region
            gc.collect()
            
            #img = resize_image(img_org, int(img_org.shape[0]*1), int(img_org.shape[1]*1))
            
            #prediction_regions_org = self.do_prediction(True, img, model_region)
            
            #prediction_regions_org = resize_image(prediction_regions_org, img_height_h, img_width_h )
            
            #prediction_regions_org = prediction_regions_org[:,:,0]
            
            #prediction_regions_org[(prediction_regions_org[:,:] == 1) & (mask_zeros_y[:,:] == 1)]=0
            #session_region.close()
            #del model_region
            #del session_region
            #gc.collect()
            
            
            
            
            mask_lines_only = (prediction_regions_org[:,:] ==3)*1
            
            mask_texts_only = (prediction_regions_org[:,:] ==1)*1
            
            mask_images_only=(prediction_regions_org[:,:] ==2)*1
            
            polygons_lines_xml, hir_lines_xml = return_contours_of_image(mask_lines_only)
            polygons_lines_xml = textline_con_fil = filter_contours_area_of_image(mask_lines_only, polygons_lines_xml, hir_lines_xml, max_area=1, min_area=0.00001)
            
            
            polygons_of_only_texts = return_contours_of_interested_region(mask_texts_only,1,0.00001)
            
            polygons_of_only_lines = return_contours_of_interested_region(mask_lines_only,1,0.00001)
            
            
            text_regions_p_true = np.zeros(prediction_regions_org.shape)
            
            text_regions_p_true = cv2.fillPoly(text_regions_p_true, pts = polygons_of_only_lines, color=(3,3,3))
            
            text_regions_p_true[:,:][mask_images_only[:,:] == 1] = 2
            
            text_regions_p_true = cv2.fillPoly(text_regions_p_true, pts = polygons_of_only_texts, color=(1,1,1))
            
            erosion_hurts = True
            K.clear_session()
            return text_regions_p_true, erosion_hurts, polygons_lines_xml

    def do_order_of_regions_full_layout(self, contours_only_text_parent, contours_only_text_parent_h, boxes, textline_mask_tot):
        self.logger.debug("enter do_order_of_regions_full_layout")
        cx_text_only, cy_text_only, x_min_text_only, _, _, _, y_cor_x_min_main = find_new_features_of_contours(contours_only_text_parent)
        cx_text_only_h, cy_text_only_h, x_min_text_only_h, _, _, _, y_cor_x_min_main_h = find_new_features_of_contours(contours_only_text_parent_h)

        try:
            arg_text_con = []
            for ii in range(len(cx_text_only)):
                for jj in range(len(boxes)):
                    if (x_min_text_only[ii] + 80) >= boxes[jj][0] and (x_min_text_only[ii] + 80) < boxes[jj][1] and y_cor_x_min_main[ii] >= boxes[jj][2] and y_cor_x_min_main[ii] < boxes[jj][3]:
                        arg_text_con.append(jj)
                        break
            args_contours = np.array(range(len(arg_text_con)))
            arg_text_con_h = []
            for ii in range(len(cx_text_only_h)):
                for jj in range(len(boxes)):
                    if (x_min_text_only_h[ii] + 80) >= boxes[jj][0] and (x_min_text_only_h[ii] + 80) < boxes[jj][1] and y_cor_x_min_main_h[ii] >= boxes[jj][2] and y_cor_x_min_main_h[ii] < boxes[jj][3]:
                        arg_text_con_h.append(jj)
                        break
            args_contours_h = np.array(range(len(arg_text_con_h)))

            order_by_con_head = np.zeros(len(arg_text_con_h))
            order_by_con_main = np.zeros(len(arg_text_con))

            ref_point = 0
            order_of_texts_tot = []
            id_of_texts_tot = []
            for iij in range(len(boxes)):

                args_contours_box = args_contours[np.array(arg_text_con) == iij]
                args_contours_box_h = args_contours_h[np.array(arg_text_con_h) == iij]
                con_inter_box = []
                con_inter_box_h = []

                for box in args_contours_box:
                    con_inter_box.append(contours_only_text_parent[box])

                for box in args_contours_box_h:
                    con_inter_box_h.append(contours_only_text_parent_h[box])

                indexes_sorted, matrix_of_orders, kind_of_texts_sorted, index_by_kind_sorted = order_of_regions(textline_mask_tot[int(boxes[iij][2]) : int(boxes[iij][3]), int(boxes[iij][0]) : int(boxes[iij][1])], con_inter_box, con_inter_box_h, boxes[iij][2])

                order_of_texts, id_of_texts = order_and_id_of_texts(con_inter_box, con_inter_box_h, matrix_of_orders, indexes_sorted, index_by_kind_sorted, kind_of_texts_sorted, ref_point)

                indexes_sorted_main = np.array(indexes_sorted)[np.array(kind_of_texts_sorted) == 1]
                indexes_by_type_main = np.array(index_by_kind_sorted)[np.array(kind_of_texts_sorted) == 1]
                indexes_sorted_head = np.array(indexes_sorted)[np.array(kind_of_texts_sorted) == 2]
                indexes_by_type_head = np.array(index_by_kind_sorted)[np.array(kind_of_texts_sorted) == 2]

                for zahler, _ in enumerate(args_contours_box):
                    arg_order_v = indexes_sorted_main[zahler]
                    order_by_con_main[args_contours_box[indexes_by_type_main[zahler]]] = np.where(indexes_sorted == arg_order_v)[0][0] + ref_point

                for zahler, _ in enumerate(args_contours_box_h):
                    arg_order_v = indexes_sorted_head[zahler]
                    order_by_con_head[args_contours_box_h[indexes_by_type_head[zahler]]] = np.where(indexes_sorted == arg_order_v)[0][0] + ref_point

                for jji in range(len(id_of_texts)):
                    order_of_texts_tot.append(order_of_texts[jji] + ref_point)
                    id_of_texts_tot.append(id_of_texts[jji])
                ref_point += len(id_of_texts)

            order_of_texts_tot = []
            for tj1 in range(len(contours_only_text_parent)):
                order_of_texts_tot.append(int(order_by_con_main[tj1]))

            for tj1 in range(len(contours_only_text_parent_h)):
                order_of_texts_tot.append(int(order_by_con_head[tj1]))

            order_text_new = []
            for iii in range(len(order_of_texts_tot)):
                order_text_new.append(np.where(np.array(order_of_texts_tot) == iii)[0][0])

        except Exception as why:
            self.logger.error(why)
            arg_text_con = []
            for ii in range(len(cx_text_only)):
                for jj in range(len(boxes)):
                    if cx_text_only[ii] >= boxes[jj][0] and cx_text_only[ii] < boxes[jj][1] and cy_text_only[ii] >= boxes[jj][2] and cy_text_only[ii] < boxes[jj][3]:  # this is valid if the center of region identify in which box it is located
                        arg_text_con.append(jj)
                        break
            args_contours = np.array(range(len(arg_text_con)))

            order_by_con_main = np.zeros(len(arg_text_con))

            ############################# head

            arg_text_con_h = []
            for ii in range(len(cx_text_only_h)):
                for jj in range(len(boxes)):
                    if cx_text_only_h[ii] >= boxes[jj][0] and cx_text_only_h[ii] < boxes[jj][1] and cy_text_only_h[ii] >= boxes[jj][2] and cy_text_only_h[ii] < boxes[jj][3]:  # this is valid if the center of region identify in which box it is located
                        arg_text_con_h.append(jj)
                        break
            args_contours_h = np.array(range(len(arg_text_con_h)))

            order_by_con_head = np.zeros(len(arg_text_con_h))

            ref_point = 0
            order_of_texts_tot = []
            id_of_texts_tot = []
            for iij, _ in enumerate(boxes):
                args_contours_box = args_contours[np.array(arg_text_con) == iij]
                args_contours_box_h = args_contours_h[np.array(arg_text_con_h) == iij]
                con_inter_box = []
                con_inter_box_h = []

                for box in args_contours_box:
                    con_inter_box.append(contours_only_text_parent[box])

                for box in args_contours_box_h:
                    con_inter_box_h.append(contours_only_text_parent_h[box])

                indexes_sorted, matrix_of_orders, kind_of_texts_sorted, index_by_kind_sorted = order_of_regions(textline_mask_tot[int(boxes[iij][2]) : int(boxes[iij][3]), int(boxes[iij][0]) : int(boxes[iij][1])], con_inter_box, con_inter_box_h, boxes[iij][2])

                order_of_texts, id_of_texts = order_and_id_of_texts(con_inter_box, con_inter_box_h, matrix_of_orders, indexes_sorted, index_by_kind_sorted, kind_of_texts_sorted, ref_point)

                indexes_sorted_main = np.array(indexes_sorted)[np.array(kind_of_texts_sorted) == 1]
                indexes_by_type_main = np.array(index_by_kind_sorted)[np.array(kind_of_texts_sorted) == 1]
                indexes_sorted_head = np.array(indexes_sorted)[np.array(kind_of_texts_sorted) == 2]
                indexes_by_type_head = np.array(index_by_kind_sorted)[np.array(kind_of_texts_sorted) == 2]

                for zahler, _ in enumerate(args_contours_box):
                    arg_order_v = indexes_sorted_main[zahler]
                    order_by_con_main[args_contours_box[indexes_by_type_main[zahler]]] = np.where(indexes_sorted == arg_order_v)[0][0] + ref_point

                for zahler, _ in enumerate(args_contours_box_h):
                    arg_order_v = indexes_sorted_head[zahler]
                    order_by_con_head[args_contours_box_h[indexes_by_type_head[zahler]]] = np.where(indexes_sorted == arg_order_v)[0][0] + ref_point

                for jji, _ in enumerate(id_of_texts):
                    order_of_texts_tot.append(order_of_texts[jji] + ref_point)
                    id_of_texts_tot.append(id_of_texts[jji])
                ref_point += len(id_of_texts)

            order_of_texts_tot = []
            for tj1 in range(len(contours_only_text_parent)):
                order_of_texts_tot.append(int(order_by_con_main[tj1]))

            for tj1 in range(len(contours_only_text_parent_h)):
                order_of_texts_tot.append(int(order_by_con_head[tj1]))

            order_text_new = []
            for iii in range(len(order_of_texts_tot)):
                order_text_new.append(np.where(np.array(order_of_texts_tot) == iii)[0][0])
        return order_text_new, id_of_texts_tot

    def do_order_of_regions_no_full_layout(self, contours_only_text_parent, contours_only_text_parent_h, boxes, textline_mask_tot):
        self.logger.debug("enter do_order_of_regions_no_full_layout")
        cx_text_only, cy_text_only, x_min_text_only, _, _, _, y_cor_x_min_main = find_new_features_of_contours(contours_only_text_parent)

        try:
            arg_text_con = []
            for ii in range(len(cx_text_only)):
                for jj in range(len(boxes)):
                    if (x_min_text_only[ii] + 80) >= boxes[jj][0] and (x_min_text_only[ii] + 80) < boxes[jj][1] and y_cor_x_min_main[ii] >= boxes[jj][2] and y_cor_x_min_main[ii] < boxes[jj][3]:
                        arg_text_con.append(jj)
                        break
            args_contours = np.array(range(len(arg_text_con)))
            order_by_con_main = np.zeros(len(arg_text_con))

            ref_point = 0
            order_of_texts_tot = []
            id_of_texts_tot = []
            for iij in range(len(boxes)):
                args_contours_box = args_contours[np.array(arg_text_con) == iij]
                con_inter_box = []
                con_inter_box_h = []
                for i in range(len(args_contours_box)):
                    con_inter_box.append(contours_only_text_parent[args_contours_box[i]])

                indexes_sorted, matrix_of_orders, kind_of_texts_sorted, index_by_kind_sorted = order_of_regions(textline_mask_tot[int(boxes[iij][2]) : int(boxes[iij][3]), int(boxes[iij][0]) : int(boxes[iij][1])], con_inter_box, con_inter_box_h, boxes[iij][2])

                order_of_texts, id_of_texts = order_and_id_of_texts(con_inter_box, con_inter_box_h, matrix_of_orders, indexes_sorted, index_by_kind_sorted, kind_of_texts_sorted, ref_point)

                indexes_sorted_main = np.array(indexes_sorted)[np.array(kind_of_texts_sorted) == 1]
                indexes_by_type_main = np.array(index_by_kind_sorted)[np.array(kind_of_texts_sorted) == 1]

                for zahler, _ in enumerate(args_contours_box):
                    arg_order_v = indexes_sorted_main[zahler]
                    order_by_con_main[args_contours_box[indexes_by_type_main[zahler]]] = np.where(indexes_sorted == arg_order_v)[0][0] + ref_point

                for jji, _ in enumerate(id_of_texts):
                    order_of_texts_tot.append(order_of_texts[jji] + ref_point)
                    id_of_texts_tot.append(id_of_texts[jji])
                ref_point += len(id_of_texts)

            order_of_texts_tot = []
            for tj1 in range(len(contours_only_text_parent)):
                order_of_texts_tot.append(int(order_by_con_main[tj1]))

            order_text_new = []
            for iii in range(len(order_of_texts_tot)):
                order_text_new.append(np.where(np.array(order_of_texts_tot) == iii)[0][0])
        
        except Exception as why:
            self.logger.error(why)
            arg_text_con = []
            for ii in range(len(cx_text_only)):
                for jj in range(len(boxes)):
                    if cx_text_only[ii] >= boxes[jj][0] and cx_text_only[ii] < boxes[jj][1] and cy_text_only[ii] >= boxes[jj][2] and cy_text_only[ii] < boxes[jj][3]:  # this is valid if the center of region identify in which box it is located
                        arg_text_con.append(jj)
                        break
            args_contours = np.array(range(len(arg_text_con)))

            order_by_con_main = np.zeros(len(arg_text_con))

            ref_point = 0
            order_of_texts_tot = []
            id_of_texts_tot = []
            for iij in range(len(boxes)):
                args_contours_box = args_contours[np.array(arg_text_con) == iij]
                con_inter_box = []
                con_inter_box_h = []

                for i in range(len(args_contours_box)):
                    con_inter_box.append(contours_only_text_parent[args_contours_box[i]])

                indexes_sorted, matrix_of_orders, kind_of_texts_sorted, index_by_kind_sorted = order_of_regions(textline_mask_tot[int(boxes[iij][2]) : int(boxes[iij][3]), int(boxes[iij][0]) : int(boxes[iij][1])], con_inter_box, con_inter_box_h, boxes[iij][2])

                order_of_texts, id_of_texts = order_and_id_of_texts(con_inter_box, con_inter_box_h, matrix_of_orders, indexes_sorted, index_by_kind_sorted, kind_of_texts_sorted, ref_point)

                indexes_sorted_main = np.array(indexes_sorted)[np.array(kind_of_texts_sorted) == 1]
                indexes_by_type_main = np.array(index_by_kind_sorted)[np.array(kind_of_texts_sorted) == 1]

                for zahler, _ in enumerate(args_contours_box):
                    arg_order_v = indexes_sorted_main[zahler]
                    order_by_con_main[args_contours_box[indexes_by_type_main[zahler]]] = np.where(indexes_sorted == arg_order_v)[0][0] + ref_point

                for jji, _ in enumerate(id_of_texts):
                    order_of_texts_tot.append(order_of_texts[jji] + ref_point)
                    id_of_texts_tot.append(id_of_texts[jji])
                ref_point += len(id_of_texts)

            order_of_texts_tot = []
            for tj1 in range(len(contours_only_text_parent)):
                order_of_texts_tot.append(int(order_by_con_main[tj1]))

            order_text_new = []
            for iii in range(len(order_of_texts_tot)):
                order_text_new.append(np.where(np.array(order_of_texts_tot) == iii)[0][0])
        
        return order_text_new, id_of_texts_tot
    def check_iou_of_bounding_box_and_contour_for_tables(self, layout, table_prediction_early, pixel_tabel, num_col_classifier):
        layout_org  = np.copy(layout)
        layout_org[:,:,0][layout_org[:,:,0]==pixel_tabel] = 0
        layout = (layout[:,:,0]==pixel_tabel)*1

        layout =np.repeat(layout[:, :, np.newaxis], 3, axis=2)
        layout = layout.astype(np.uint8)
        imgray = cv2.cvtColor(layout, cv2.COLOR_BGR2GRAY )
        _, thresh = cv2.threshold(imgray, 0, 255, 0)

        contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        cnt_size = np.array([cv2.contourArea(contours[j]) for j in range(len(contours))])
        
        contours_new = []
        for i in range(len(contours)):
            x, y, w, h = cv2.boundingRect(contours[i])
            iou = cnt_size[i] /float(w*h) *100
            
            if iou<80:
                layout_contour = np.zeros((layout_org.shape[0], layout_org.shape[1]))
                layout_contour= cv2.fillPoly(layout_contour,pts=[contours[i]] ,color=(1,1,1))
                
                
                layout_contour_sum = layout_contour.sum(axis=0)
                layout_contour_sum_diff = np.diff(layout_contour_sum)
                layout_contour_sum_diff= np.abs(layout_contour_sum_diff)
                layout_contour_sum_diff_smoothed= gaussian_filter1d(layout_contour_sum_diff, 10)

                peaks, _ = find_peaks(layout_contour_sum_diff_smoothed, height=0)
                peaks= peaks[layout_contour_sum_diff_smoothed[peaks]>4]
                
                for j in range(len(peaks)):
                    layout_contour[:,peaks[j]-3+1:peaks[j]+1+3] = 0
                    
                layout_contour=cv2.erode(layout_contour[:,:], KERNEL, iterations=5)
                layout_contour=cv2.dilate(layout_contour[:,:], KERNEL, iterations=5)
                
                layout_contour =np.repeat(layout_contour[:, :, np.newaxis], 3, axis=2)
                layout_contour = layout_contour.astype(np.uint8)
                
                imgray = cv2.cvtColor(layout_contour, cv2.COLOR_BGR2GRAY )
                _, thresh = cv2.threshold(imgray, 0, 255, 0)

                contours_sep, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

                for ji in range(len(contours_sep) ):
                    contours_new.append(contours_sep[ji])
                    if num_col_classifier>=2:
                        only_recent_contour_image = np.zeros((layout.shape[0],layout.shape[1]))
                        only_recent_contour_image= cv2.fillPoly(only_recent_contour_image,pts=[contours_sep[ji]] ,color=(1,1,1))
                        table_pixels_masked_from_early_pre = only_recent_contour_image[:,:]*table_prediction_early[:,:]
                        iou_in = table_pixels_masked_from_early_pre.sum() /float(only_recent_contour_image.sum()) *100
                        #print(iou_in,'iou_in_in1')
                        
                        if iou_in>30:
                            layout_org= cv2.fillPoly(layout_org,pts=[contours_sep[ji]] ,color=(pixel_tabel,pixel_tabel,pixel_tabel))
                        else:
                            pass
                    else:
                        
                        layout_org= cv2.fillPoly(layout_org,pts=[contours_sep[ji]] ,color=(pixel_tabel,pixel_tabel,pixel_tabel))
                
            else:
                contours_new.append(contours[i])
                if num_col_classifier>=2:
                    only_recent_contour_image = np.zeros((layout.shape[0],layout.shape[1]))
                    only_recent_contour_image= cv2.fillPoly(only_recent_contour_image,pts=[contours[i]] ,color=(1,1,1))
                    
                    table_pixels_masked_from_early_pre = only_recent_contour_image[:,:]*table_prediction_early[:,:]
                    iou_in = table_pixels_masked_from_early_pre.sum() /float(only_recent_contour_image.sum()) *100
                    #print(iou_in,'iou_in')
                    if iou_in>30:
                        layout_org= cv2.fillPoly(layout_org,pts=[contours[i]] ,color=(pixel_tabel,pixel_tabel,pixel_tabel))
                    else:
                        pass
                else:
                    layout_org= cv2.fillPoly(layout_org,pts=[contours[i]] ,color=(pixel_tabel,pixel_tabel,pixel_tabel))
                
        return layout_org, contours_new
    def delete_separator_around(self,spliter_y,peaks_neg,image_by_region, pixel_line, pixel_table):
        # format of subboxes: box=[x1, x2 , y1, y2]
        pix_del = 100
        if len(image_by_region.shape)==3:
            for i in range(len(spliter_y)-1):
                for j in range(1,len(peaks_neg[i])-1):
                    image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0]==pixel_line ]=0
                    image_by_region[spliter_y[i]:spliter_y[i+1],peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,1]==pixel_line ]=0
                    image_by_region[spliter_y[i]:spliter_y[i+1],peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,2]==pixel_line ]=0
                    
                    image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0]==pixel_table ]=0
                    image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,1]==pixel_table ]=0
                    image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,0][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del,2]==pixel_table ]=0
        else:
            for i in range(len(spliter_y)-1):
                for j in range(1,len(peaks_neg[i])-1):
                    image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del]==pixel_line ]=0
                    
                    image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del][image_by_region[int(spliter_y[i]):int(spliter_y[i+1]),peaks_neg[i][j]-pix_del:peaks_neg[i][j]+pix_del]==pixel_table ]=0
        return image_by_region
    def add_tables_heuristic_to_layout(self, image_regions_eraly_p,boxes, slope_mean_hor, spliter_y,peaks_neg_tot, image_revised, num_col_classifier, min_area, pixel_line):
        pixel_table =10
        image_revised_1 = self.delete_separator_around(spliter_y, peaks_neg_tot, image_revised, pixel_line, pixel_table)
        
        try:
            image_revised_1[:,:30][image_revised_1[:,:30]==pixel_line] = 0
            image_revised_1[:,image_revised_1.shape[1]-30:][image_revised_1[:,image_revised_1.shape[1]-30:]==pixel_line] = 0
        except:
            pass
        
        img_comm_e = np.zeros(image_revised_1.shape)
        img_comm = np.repeat(img_comm_e[:, :, np.newaxis], 3, axis=2)

        for indiv in np.unique(image_revised_1):
            image_col=(image_revised_1==indiv)*255
            img_comm_in=np.repeat(image_col[:, :, np.newaxis], 3, axis=2)
            img_comm_in=img_comm_in.astype(np.uint8)

            imgray = cv2.cvtColor(img_comm_in, cv2.COLOR_BGR2GRAY)
            ret, thresh = cv2.threshold(imgray, 0, 255, 0)
            contours,hirarchy=cv2.findContours(thresh.copy(), cv2.RETR_TREE,cv2.CHAIN_APPROX_SIMPLE)

            if indiv==pixel_table:
                main_contours = filter_contours_area_of_image_tables(thresh, contours, hirarchy, max_area = 1, min_area = 0.001)
            else:
                main_contours = filter_contours_area_of_image_tables(thresh, contours, hirarchy, max_area = 1, min_area = min_area)

            img_comm = cv2.fillPoly(img_comm, pts = main_contours, color = (indiv, indiv, indiv))
            img_comm = img_comm.astype(np.uint8)
            
        if not self.isNaN(slope_mean_hor):
            image_revised_last = np.zeros((image_regions_eraly_p.shape[0], image_regions_eraly_p.shape[1],3))
            for i in range(len(boxes)):
                image_box=img_comm[int(boxes[i][2]):int(boxes[i][3]),int(boxes[i][0]):int(boxes[i][1]),:]
                try:
                    image_box_tabels_1=(image_box[:,:,0]==pixel_table)*1
                    contours_tab,_=return_contours_of_image(image_box_tabels_1)
                    contours_tab=filter_contours_area_of_image_tables(image_box_tabels_1,contours_tab,_,1,0.003)
                    image_box_tabels_1=(image_box[:,:,0]==pixel_line)*1

                    image_box_tabels_and_m_text=( (image_box[:,:,0]==pixel_table) | (image_box[:,:,0]==1) )*1
                    image_box_tabels_and_m_text=image_box_tabels_and_m_text.astype(np.uint8)

                    image_box_tabels_1=image_box_tabels_1.astype(np.uint8)
                    image_box_tabels_1 = cv2.dilate(image_box_tabels_1,KERNEL,iterations = 5)

                    contours_table_m_text,_=return_contours_of_image(image_box_tabels_and_m_text)
                    image_box_tabels=np.repeat(image_box_tabels_1[:, :, np.newaxis], 3, axis=2)

                    image_box_tabels=image_box_tabels.astype(np.uint8)
                    imgray = cv2.cvtColor(image_box_tabels, cv2.COLOR_BGR2GRAY)
                    ret, thresh = cv2.threshold(imgray, 0, 255, 0)

                    contours_line,hierachy=cv2.findContours(thresh,cv2.RETR_TREE,cv2.CHAIN_APPROX_SIMPLE)

                    y_min_main_line ,y_max_main_line=find_features_of_contours(contours_line)
                    y_min_main_tab ,y_max_main_tab=find_features_of_contours(contours_tab)

                    cx_tab_m_text,cy_tab_m_text ,x_min_tab_m_text , x_max_tab_m_text, y_min_tab_m_text ,y_max_tab_m_text, _= find_new_features_of_contours(contours_table_m_text)
                    cx_tabl,cy_tabl ,x_min_tabl , x_max_tabl, y_min_tabl ,y_max_tabl,_= find_new_features_of_contours(contours_tab)

                    if len(y_min_main_tab )>0:
                        y_down_tabs=[]
                        y_up_tabs=[]

                        for i_t in range(len(y_min_main_tab )):
                            y_down_tab=[]
                            y_up_tab=[]
                            for i_l in range(len(y_min_main_line)):
                                if y_min_main_tab[i_t]>y_min_main_line[i_l] and  y_max_main_tab[i_t]>y_min_main_line[i_l] and y_min_main_tab[i_t]>y_max_main_line[i_l] and y_max_main_tab[i_t]>y_min_main_line[i_l]:
                                    pass
                                elif y_min_main_tab[i_t]<y_max_main_line[i_l] and y_max_main_tab[i_t]<y_max_main_line[i_l] and y_max_main_tab[i_t]<y_min_main_line[i_l] and y_min_main_tab[i_t]<y_min_main_line[i_l]:
                                    pass
                                elif np.abs(y_max_main_line[i_l]-y_min_main_line[i_l])<100:
                                    pass
                                else:
                                    y_up_tab.append(np.min([y_min_main_line[i_l], y_min_main_tab[i_t] ])  )
                                    y_down_tab.append( np.max([ y_max_main_line[i_l],y_max_main_tab[i_t] ]) )

                            if len(y_up_tab)==0:
                                y_up_tabs.append(y_min_main_tab[i_t])
                                y_down_tabs.append(y_max_main_tab[i_t])
                            else:
                                y_up_tabs.append(np.min(y_up_tab))
                                y_down_tabs.append(np.max(y_down_tab))
                    else:
                        y_down_tabs=[]
                        y_up_tabs=[]
                        pass
                except:
                    y_down_tabs=[]
                    y_up_tabs=[]

                for ii in range(len(y_up_tabs)):
                    image_box[y_up_tabs[ii]:y_down_tabs[ii],:,0]=pixel_table

                image_revised_last[int(boxes[i][2]):int(boxes[i][3]),int(boxes[i][0]):int(boxes[i][1]),:]=image_box[:,:,:]
        else:
            for i in range(len(boxes)):

                image_box=img_comm[int(boxes[i][2]):int(boxes[i][3]),int(boxes[i][0]):int(boxes[i][1]),:]
                image_revised_last[int(boxes[i][2]):int(boxes[i][3]),int(boxes[i][0]):int(boxes[i][1]),:]=image_box[:,:,:]
        
        if num_col_classifier==1:
            img_tables_col_1=( image_revised_last[:,:,0]==pixel_table )*1
            img_tables_col_1=img_tables_col_1.astype(np.uint8)
            contours_table_col1,_=return_contours_of_image(img_tables_col_1)
            
            _,_ ,_ , _, y_min_tab_col1 ,y_max_tab_col1, _= find_new_features_of_contours(contours_table_col1)
            
            if len(y_min_tab_col1)>0:
                for ijv in range(len(y_min_tab_col1)):
                    image_revised_last[int(y_min_tab_col1[ijv]):int(y_max_tab_col1[ijv]),:,:]=pixel_table
        return image_revised_last
    def do_order_of_regions(self, *args, **kwargs):
        if self.full_layout:
            return self.do_order_of_regions_full_layout(*args, **kwargs)
        return self.do_order_of_regions_no_full_layout(*args, **kwargs)
    
    def get_tables_from_model(self, img, num_col_classifier):
        img_org = np.copy(img)
        
        img_height_h = img_org.shape[0]
        img_width_h = img_org.shape[1]
        
        model_region, session_region = self.start_new_session_and_model(self.model_tables)
        
        patches = False
        
        if num_col_classifier < 4 and num_col_classifier > 2:
            prediction_table = self.do_prediction(patches, img, model_region)
            pre_updown = self.do_prediction(patches, cv2.flip(img[:,:,:], -1), model_region)
            pre_updown = cv2.flip(pre_updown, -1)
            
            prediction_table[:,:,0][pre_updown[:,:,0]==1]=1
            prediction_table = prediction_table.astype(np.int16)
            
        elif num_col_classifier ==2:
            height_ext = 0#int( img.shape[0]/4. )
            h_start = int(height_ext/2.)
            width_ext = int( img.shape[1]/8. )
            w_start = int(width_ext/2.)
        
            height_new = img.shape[0]+height_ext
            width_new = img.shape[1]+width_ext
            
            img_new =np.ones((height_new,width_new,img.shape[2])).astype(float)*0
            img_new[h_start:h_start+img.shape[0] ,w_start: w_start+img.shape[1], : ] =img[:,:,:]
            
            prediction_ext = self.do_prediction(patches, img_new, model_region)
            
            pre_updown = self.do_prediction(patches, cv2.flip(img_new[:,:,:], -1), model_region)
            pre_updown = cv2.flip(pre_updown, -1)
            
            prediction_table = prediction_ext[h_start:h_start+img.shape[0] ,w_start: w_start+img.shape[1], : ]
            prediction_table_updown = pre_updown[h_start:h_start+img.shape[0] ,w_start: w_start+img.shape[1], : ]
            
            prediction_table[:,:,0][prediction_table_updown[:,:,0]==1]=1
            prediction_table = prediction_table.astype(np.int16)

        elif num_col_classifier ==1:
            height_ext = 0# int( img.shape[0]/4. )
            h_start = int(height_ext/2.)
            width_ext = int( img.shape[1]/4. )
            w_start = int(width_ext/2.)
        
            height_new = img.shape[0]+height_ext
            width_new = img.shape[1]+width_ext
            
            img_new =np.ones((height_new,width_new,img.shape[2])).astype(float)*0
            img_new[h_start:h_start+img.shape[0] ,w_start: w_start+img.shape[1], : ] =img[:,:,:]
            
            prediction_ext = self.do_prediction(patches, img_new, model_region)
            
            pre_updown = self.do_prediction(patches, cv2.flip(img_new[:,:,:], -1), model_region)
            pre_updown = cv2.flip(pre_updown, -1)
            
            prediction_table = prediction_ext[h_start:h_start+img.shape[0] ,w_start: w_start+img.shape[1], : ]
            prediction_table_updown = pre_updown[h_start:h_start+img.shape[0] ,w_start: w_start+img.shape[1], : ]
            
            prediction_table[:,:,0][prediction_table_updown[:,:,0]==1]=1
            prediction_table = prediction_table.astype(np.int16)

        else:
            prediction_table = np.zeros(img.shape)
            img_w_half = int(img.shape[1]/2.)
            
            pre1 = self.do_prediction(patches, img[:,0:img_w_half,:], model_region)
            pre2 = self.do_prediction(patches, img[:,img_w_half:,:], model_region)
            
            pre_full = self.do_prediction(patches, img[:,:,:], model_region)
            
            pre_updown = self.do_prediction(patches, cv2.flip(img[:,:,:], -1), model_region)
            pre_updown = cv2.flip(pre_updown, -1)
            
            prediction_table_full_erode = cv2.erode(pre_full[:,:,0], KERNEL, iterations=4)
            prediction_table_full_erode = cv2.dilate(prediction_table_full_erode, KERNEL, iterations=4)
            
            prediction_table_full_updown_erode = cv2.erode(pre_updown[:,:,0], KERNEL, iterations=4)
            prediction_table_full_updown_erode = cv2.dilate(prediction_table_full_updown_erode, KERNEL, iterations=4)

            prediction_table[:,0:img_w_half,:] = pre1[:,:,:]
            prediction_table[:,img_w_half:,:] = pre2[:,:,:]
            
            prediction_table[:,:,0][prediction_table_full_erode[:,:]==1]=1
            prediction_table[:,:,0][prediction_table_full_updown_erode[:,:]==1]=1
            prediction_table = prediction_table.astype(np.int16)
            
        #prediction_table_erode = cv2.erode(prediction_table[:,:,0], self.kernel, iterations=6)
        #prediction_table_erode = cv2.dilate(prediction_table_erode, self.kernel, iterations=6)
        
        prediction_table_erode = cv2.erode(prediction_table[:,:,0], KERNEL, iterations=20)
        prediction_table_erode = cv2.dilate(prediction_table_erode, KERNEL, iterations=20)

        del model_region
        del session_region
        gc.collect()
        
        
        return prediction_table_erode.astype(np.int16)
    
    def return_modified_lower_limits_image_new(self,img_lower,img_lower_bound):
        img_lower_limits  = (img_lower[:,:]==1)*1

        #plt.figure(figsize=(20,20))
        #plt.imshow(img_lower_limits)
        #plt.show()
        
        contoures_limits_interest = return_contours_of_interested_region_by_min_size(img_lower_limits,1,0)
        
        img_lower_limits = np.zeros(img_lower_limits.shape)
        
        img_lower_limits  =  cv2.fillPoly(img_lower_limits, pts=contoures_limits_interest, color=(1, 1))

        #print(len(contoures_limits_interest),'contoures_limits_interest')
        cx_limits, cy_limits, x_min_limits, x_max_limits, y_min_limits, y_max_limits, _ = find_new_features_of_contours(contoures_limits_interest)
        index_limits = np.array(range(len(contoures_limits_interest)))
        
        args_lower = np.array(range(len(contoures_limits_interest)))
        
        img_lower_limits_sum = img_lower_limits.sum(axis=0)
        
        #plt.plot(img_lower_limits_sum)
        #plt.show()
        
        indexes_tot_width = np.array(range(len(img_lower_limits_sum)))
        indexes_tot_width_zero = indexes_tot_width[img_lower_limits_sum==0]
        #indexes_tot_width_zero_true = indexes_tot_width_zero[(indexes_tot_width_zero>=x_min_line_interest) & (indexes_tot_width_zero<x_max_line_interest)]
        
        indexes_tot_width_zero_true_diff = np.diff(indexes_tot_width_zero)
        indexes_diff_not_one = np.array(range(len(indexes_tot_width_zero_true_diff)))[indexes_tot_width_zero_true_diff!=1]
        
        #print(indexes_tot_width_zero,'indexes_tot_width_zero')
        img_lower_limits[:,indexes_tot_width_zero]=img_lower_bound[:,indexes_tot_width_zero]
        #img_lower_limits[lower_mean_y+5-5:lower_mean_y+5,indexes_tot_width_zero_true]=1
        if len(indexes_tot_width_zero)>0:

            img_lower_limits[: ,indexes_tot_width_zero[0]-5:indexes_tot_width_zero[0]+5 ] =1
            img_lower_limits[: ,indexes_tot_width_zero[len(indexes_tot_width_zero)-1]-5:indexes_tot_width_zero[len(indexes_tot_width_zero)-1]+5 ] =1
        
        for ind in indexes_diff_not_one:
            img_lower_limits[: ,indexes_tot_width_zero[ind]-5:indexes_tot_width_zero[ind]+5 ] =1
            img_lower_limits[: ,indexes_tot_width_zero[ind+1]-5:indexes_tot_width_zero[ind+1]+5 ] =1
            
        
        #print(args_lower,'args_lowerargs_lowerargs_lower')
        #print(x_min_limits[args_lower],'x_min_limits[args_lower]x_min_limits[args_lower]')
        #print(x_max_limits[args_lower],'x_max_limits[args_lower]x_max_limits[args_lower]')
        #plt.imshow(img_lower_limits)
        #plt.show()
        
        
        ind_lims_lower_all = np.array(range(len(contoures_limits_interest)))
        for ind_lim in args_lower:
            xmin_xmin_lims = x_min_limits[ind_lim]-x_min_limits[args_lower]
            xmax_xmax_lims = x_max_limits[ind_lim]-x_max_limits[args_lower]
            xmax_xmin_lims = x_max_limits[ind_lim]-x_min_limits[args_lower]
            xmin_xmax_lims = x_min_limits[ind_lim]-x_max_limits[args_lower]

            args_interest = ind_lims_lower_all[(xmin_xmin_lims<0) & (xmax_xmax_lims<0) & (xmax_xmin_lims>=-1)]
            args_interest2 = ind_lims_lower_all[(xmin_xmin_lims>0) & (xmax_xmax_lims>0) & (xmin_xmax_lims<=1)]
            
            #print(args_interest,'args_interest')
            #print(args_interest2,'args_interest2')
            
            if len(args_interest)>1:
                max_y_of_lims= np.argmax( [cy_limits[ind_lim]]+list(np.array(cy_limits)[args_lower[args_interest] ] ) )
                #print(max_y_of_lims)
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest[max_y_of_lims-1]]
                    x_interest_be_filled = x_min_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_max_limits[arg_x_int]
            elif len(args_interest)==1:
                max_y_of_lims= np.argmax( [cy_limits[ind_lim]]+[np.array(cy_limits)[args_lower[args_interest] ] ] )
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest[max_y_of_lims-1]]
                    x_interest_be_filled = x_min_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_max_limits[arg_x_int]
                    #print(args_interest[max_y_of_lims-1])
                #print(max_y_of_lims)
                img_lower_limits[: ,x_interest_be_filled-5:x_interest_be_filled+5 ] =1 
                #plt.figure(figsize=(20,20))
                #plt.imshow(img_lower_limits)
                #plt.show()
            else:
                pass
            
            
            if len(args_interest2)>1:
                max_y_of_lims= np.argmax( [cy_limits[ind_lim]]+list(np.array(cy_limits)[args_lower[args_interest2] ] ) )
                #print(max_y_of_lims)
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest2[max_y_of_lims-1]]
                    x_interest_be_filled = x_max_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_min_limits[arg_x_int]
            elif len(args_interest2)==1:
                max_y_of_lims= np.argmax( [cy_limits[ind_lim]]+[np.array(cy_limits)[args_lower[args_interest2] ] ] )
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest2[max_y_of_lims-1]]
                    x_interest_be_filled = x_max_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_min_limits[arg_x_int]
                    #print(args_interest[max_y_of_lims-1])
                #print(max_y_of_lims)
                img_lower_limits[: ,x_interest_be_filled-5:x_interest_be_filled+5 ] =1 
                #plt.figure(figsize=(20,20))
                #plt.imshow(img_lower_limits)
                #plt.show()
            else:
                pass
            #max_y_of_lims= np.argmax([cy_lim[ind_lim]])
        #plt.figure(figsize=(20,20))
        #plt.imshow(img_lower_limits)
        #plt.show()
        #img_lower_limits = cv2.dilate(img_lower_limits, KERNEL, iterations=2)
        #img_lower_limits = cv2.erode(img_lower_limits, KERNEL, iterations=2)
        
        return img_lower_limits

    def return_modified_upper_limits_image_new(self, img_lower,img_lower_bound):
        img_lower_limits  = (img_lower[:,:]==1)*1

        #plt.figure(figsize=(20,20))
        #plt.imshow(img_lower_limits)
        #plt.show()
        
        contoures_limits_interest = return_contours_of_interested_region_by_min_size(img_lower_limits,1,0)
        
        img_lower_limits = np.zeros(img_lower_limits.shape)
        
        img_lower_limits  =  cv2.fillPoly(img_lower_limits, pts=contoures_limits_interest, color=(1, 1))
        #print(len(contoures_limits_interest),'contoures_limits_interestcontoures_limits_interest')

        cx_limits, cy_limits, x_min_limits, x_max_limits, y_min_limits, y_max_limits, _ = find_new_features_of_contours(contoures_limits_interest)
        
        #print(cx_limits,'cx_limitscx_limitscx_limits')
        #print(cy_limits,'cy_limitscy_limitscy_limits')
        index_limits = np.array(range(len(contoures_limits_interest)))
        
        args_lower = np.array(range(len(contoures_limits_interest)))
        
        img_lower_limits_sum = img_lower_limits.sum(axis=0)
        
        #plt.plot(img_lower_limits_sum)
        #plt.show()
        
        indexes_tot_width = np.array(range(len(img_lower_limits_sum)))
        indexes_tot_width_zero = indexes_tot_width[img_lower_limits_sum==0]
        #indexes_tot_width_zero_true = indexes_tot_width_zero[(indexes_tot_width_zero>=x_min_line_interest) & (indexes_tot_width_zero<x_max_line_interest)]
        
        indexes_tot_width_zero_true_diff = np.diff(indexes_tot_width_zero)
        indexes_diff_not_one = np.array(range(len(indexes_tot_width_zero_true_diff)))[indexes_tot_width_zero_true_diff!=1]
        
        #plt.figure(figsize=(20,20))
        #plt.imshow(img_lower_bound)
        #plt.show()
        #print(indexes_tot_width_zero,'indexes_tot_width_zero')
        img_lower_limits[:,indexes_tot_width_zero]=img_lower_bound[:,indexes_tot_width_zero]
        #img_lower_limits[lower_mean_y+5-5:lower_mean_y+5,indexes_tot_width_zero_true]=1
        
        #plt.figure(figsize=(20,20))
        #plt.imshow(img_lower_limits)
        #plt.show()
        if len(indexes_tot_width_zero)>0:

            img_lower_limits[: ,indexes_tot_width_zero[0]-5:indexes_tot_width_zero[0]+5 ] =1
            img_lower_limits[: ,indexes_tot_width_zero[len(indexes_tot_width_zero)-1]-5:indexes_tot_width_zero[len(indexes_tot_width_zero)-1]+5 ] =1
        
        for ind in indexes_diff_not_one:
            img_lower_limits[: ,indexes_tot_width_zero[ind]-5:indexes_tot_width_zero[ind]+5 ] =1
            img_lower_limits[: ,indexes_tot_width_zero[ind+1]-5:indexes_tot_width_zero[ind+1]+5 ] =1
            
        #plt.figure(figsize=(20,20))
        #plt.imshow(img_lower_limits)
        #plt.show()
        #print(args_lower,'args_lowerargs_lowerargs_lower')
        #print(x_min_limits[args_lower],'x_min_limits[args_lower]x_min_limits[args_lower]')
        #print(x_max_limits[args_lower],'x_max_limits[args_lower]x_max_limits[args_lower]')
        #plt.imshow(img_lower_limits)
        #plt.show()
        
        
        ind_lims_lower_all = np.array(range(len(contoures_limits_interest)))
        for ind_lim in args_lower:
            xmin_xmin_lims = x_min_limits[ind_lim]-x_min_limits[args_lower]
            xmax_xmax_lims = x_max_limits[ind_lim]-x_max_limits[args_lower]
            xmax_xmin_lims = x_max_limits[ind_lim]-x_min_limits[args_lower]
            xmin_xmax_lims = x_min_limits[ind_lim]-x_max_limits[args_lower]

            args_interest = ind_lims_lower_all[(xmin_xmin_lims<0) & (xmax_xmax_lims<0) & (xmax_xmin_lims>=-1)]
            args_interest2 = ind_lims_lower_all[(xmin_xmin_lims>0) & (xmax_xmax_lims>0) & (xmin_xmax_lims<=1)]
            
            #print(args_interest,'args_interest')
            #print(args_interest2,'args_interest2')
            
            if len(args_interest)>1:
                max_y_of_lims= np.argmin( [cy_limits[ind_lim]]+list(np.array(cy_limits)[args_lower[args_interest] ] ) )
                #print(max_y_of_lims)
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest[max_y_of_lims-1]]
                    x_interest_be_filled = x_min_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_max_limits[arg_x_int]
            elif len(args_interest)==1:
                max_y_of_lims= np.argmin( [cy_limits[ind_lim]]+[np.array(cy_limits)[args_lower[args_interest] ] ] )
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest[max_y_of_lims-1]]
                    x_interest_be_filled = x_min_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_max_limits[arg_x_int]
                    #print(args_interest[max_y_of_lims-1])
                #print(max_y_of_lims)
                img_lower_limits[: ,x_interest_be_filled-5:x_interest_be_filled+5 ] =1 
                #plt.figure(figsize=(20,20))
                #plt.imshow(img_lower_limits)
                #plt.show()
            else:
                pass
            
            
            if len(args_interest2)>1:
                max_y_of_lims= np.argmin( [cy_limits[ind_lim]]+list(np.array(cy_limits)[args_lower[args_interest2] ] ) )
                #print(max_y_of_lims)
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest2[max_y_of_lims-1]]
                    x_interest_be_filled = x_max_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_min_limits[arg_x_int]
            elif len(args_interest2)==1:
                max_y_of_lims= np.argmin( [cy_limits[ind_lim]]+[np.array(cy_limits)[args_lower[args_interest2] ] ] )
                if max_y_of_lims>=1:
                    arg_x_int = index_limits[args_interest2[max_y_of_lims-1]]
                    x_interest_be_filled = x_max_limits[arg_x_int]
                    #print(x_interest_be_filled)
                else:
                    arg_x_int = index_limits[ind_lim]
                    x_interest_be_filled = x_min_limits[arg_x_int]
                    #print(args_interest[max_y_of_lims-1])
                #print(max_y_of_lims)
                img_lower_limits[: ,x_interest_be_filled-5:x_interest_be_filled+5 ] =1 
                #plt.figure(figsize=(20,20))
                #plt.imshow(img_lower_limits)
                #plt.show()
            else:
                pass
            #max_y_of_lims= np.argmax([cy_lim[ind_lim]])
        #plt.figure(figsize=(20,20))
        #plt.imshow(img_lower_limits)
        #plt.show()
        #img_lower_limits = cv2.dilate(img_lower_limits, KERNEL, iterations=2)
        #img_lower_limits = cv2.erode(img_lower_limits, KERNEL, iterations=2)
        
        return img_lower_limits

    def run_graphics_and_columns_light(self, text_regions_p_1, textline_mask_tot_ea, num_col_classifier, num_column_is_classified, erosion_hurts):
        img_g = self.imread(grayscale=True, uint8=True)

        img_g3 = np.zeros((img_g.shape[0], img_g.shape[1], 3))
        img_g3 = img_g3.astype(np.uint8)
        img_g3[:, :, 0] = img_g[:, :]
        img_g3[:, :, 1] = img_g[:, :]
        img_g3[:, :, 2] = img_g[:, :]

        image_page, page_coord, cont_page = self.extract_page()
        
        if self.tables:
            table_prediction = self.get_tables_from_model(image_page, num_col_classifier)
        else:
            table_prediction = (np.zeros((image_page.shape[0], image_page.shape[1]))).astype(np.int16)
        
        if self.plotter:
            self.plotter.save_page_image(image_page)

        text_regions_p_1 = text_regions_p_1[page_coord[0] : page_coord[1], page_coord[2] : page_coord[3]]
        textline_mask_tot_ea = textline_mask_tot_ea[page_coord[0] : page_coord[1], page_coord[2] : page_coord[3]]
        mask_images = (text_regions_p_1[:, :] == 2) * 1
        mask_images = mask_images.astype(np.uint8)
        mask_images = cv2.erode(mask_images[:, :], KERNEL, iterations=10)
        mask_lines = (text_regions_p_1[:, :] == 3) * 1
        mask_lines = mask_lines.astype(np.uint8)
        img_only_regions_with_sep = ((text_regions_p_1[:, :] != 3) & (text_regions_p_1[:, :] != 0)) * 1
        img_only_regions_with_sep = img_only_regions_with_sep.astype(np.uint8)
        
        
        if erosion_hurts:
            img_only_regions = np.copy(img_only_regions_with_sep[:,:])
        else:
            img_only_regions = cv2.erode(img_only_regions_with_sep[:,:], KERNEL, iterations=6)
        
        ##print(img_only_regions.shape,'img_only_regions')
        ##plt.imshow(img_only_regions[:,:])
        ##plt.show()
        num_col, _ = find_num_col(img_only_regions, num_col_classifier, self.tables, multiplier=6.0)
        try:
            num_col, _ = find_num_col(img_only_regions, num_col_classifier, self.tables, multiplier=6.0)
            num_col = num_col + 1
            if not num_column_is_classified:
                num_col_classifier = num_col + 1
        except Exception as why:
            self.logger.error(why)
            num_col = None
        return num_col, num_col_classifier, img_only_regions, page_coord, image_page, mask_images, mask_lines, text_regions_p_1, cont_page, table_prediction, textline_mask_tot_ea
    def run_graphics_and_columns(self, text_regions_p_1, num_col_classifier, num_column_is_classified, erosion_hurts):
        img_g = self.imread(grayscale=True, uint8=True)

        img_g3 = np.zeros((img_g.shape[0], img_g.shape[1], 3))
        img_g3 = img_g3.astype(np.uint8)
        img_g3[:, :, 0] = img_g[:, :]
        img_g3[:, :, 1] = img_g[:, :]
        img_g3[:, :, 2] = img_g[:, :]

        image_page, page_coord, cont_page = self.extract_page()
        
        if self.tables:
            table_prediction = self.get_tables_from_model(image_page, num_col_classifier)
        else:
            table_prediction = (np.zeros((image_page.shape[0], image_page.shape[1]))).astype(np.int16)
        
        if self.plotter:
            self.plotter.save_page_image(image_page)

        text_regions_p_1 = text_regions_p_1[page_coord[0] : page_coord[1], page_coord[2] : page_coord[3]]
        mask_images = (text_regions_p_1[:, :] == 2) * 1
        mask_images = mask_images.astype(np.uint8)
        mask_images = cv2.erode(mask_images[:, :], KERNEL, iterations=10)
        mask_lines = (text_regions_p_1[:, :] == 3) * 1
        mask_lines = mask_lines.astype(np.uint8)
        img_only_regions_with_sep = ((text_regions_p_1[:, :] != 3) & (text_regions_p_1[:, :] != 0)) * 1
        img_only_regions_with_sep = img_only_regions_with_sep.astype(np.uint8)
        
        
        if erosion_hurts:
            img_only_regions = np.copy(img_only_regions_with_sep[:,:])
        else:
            img_only_regions = cv2.erode(img_only_regions_with_sep[:,:], KERNEL, iterations=6)
            
        
        try:
            num_col, _ = find_num_col(img_only_regions, num_col_classifier, self.tables, multiplier=6.0)
            num_col = num_col + 1
            if not num_column_is_classified:
                num_col_classifier = num_col + 1
        except Exception as why:
            self.logger.error(why)
            num_col = None
        return num_col, num_col_classifier, img_only_regions, page_coord, image_page, mask_images, mask_lines, text_regions_p_1, cont_page, table_prediction

    def run_enhancement(self,light_version):
        self.logger.info("Resizing and enhancing image...")
        is_image_enhanced, img_org, img_res, num_col_classifier, num_column_is_classified, img_bin = self.resize_and_enhance_image_with_column_classifier(light_version)
        self.logger.info("Image was %senhanced.", '' if is_image_enhanced else 'not ')
        K.clear_session()
        scale = 1
        if is_image_enhanced:
            if self.allow_enhancement:
                img_res = img_res.astype(np.uint8)
                self.get_image_and_scales(img_org, img_res, scale)
                if self.plotter:
                    self.plotter.save_enhanced_image(img_res)
            else:
                self.get_image_and_scales_after_enhancing(img_org, img_res)
        else:
            if self.allow_enhancement:
                self.get_image_and_scales(img_org, img_res, scale)
            else:
                self.get_image_and_scales(img_org, img_res, scale)
            if self.allow_scaling:
                img_org, img_res, is_image_enhanced = self.resize_image_with_column_classifier(is_image_enhanced, img_bin)
                self.get_image_and_scales_after_enhancing(img_org, img_res)
        return img_res, is_image_enhanced, num_col_classifier, num_column_is_classified

    def run_textline(self, image_page):
        scaler_h_textline = 1  # 1.2#1.2
        scaler_w_textline = 1  # 0.9#1
        textline_mask_tot_ea, _ = self.textline_contours(image_page, True, scaler_h_textline, scaler_w_textline)
        K.clear_session()
        if self.plotter:
            self.plotter.save_plot_of_textlines(textline_mask_tot_ea, image_page)
        return textline_mask_tot_ea

    def run_deskew(self, textline_mask_tot_ea):
        sigma = 2
        main_page_deskew = True
        slope_deskew = return_deskew_slop(cv2.erode(textline_mask_tot_ea, KERNEL, iterations=2), sigma, main_page_deskew, plotter=self.plotter)
        slope_first = 0

        if self.plotter:
            self.plotter.save_deskewed_image(slope_deskew)
        self.logger.info("slope_deskew: %s", slope_deskew)
        return slope_deskew, slope_first

    def run_marginals(self, image_page, textline_mask_tot_ea, mask_images, mask_lines, num_col_classifier, slope_deskew, text_regions_p_1, table_prediction):
        image_page_rotated, textline_mask_tot = image_page[:, :], textline_mask_tot_ea[:, :]
        textline_mask_tot[mask_images[:, :] == 1] = 0

        text_regions_p_1[mask_lines[:, :] == 1] = 3
        text_regions_p = text_regions_p_1[:, :]
        text_regions_p = np.array(text_regions_p)

        if num_col_classifier in (1, 2):
            try:
                regions_without_separators = (text_regions_p[:, :] == 1) * 1
                if self.tables:
                    regions_without_separators[table_prediction==1] = 1
                regions_without_separators = regions_without_separators.astype(np.uint8)
                text_regions_p = get_marginals(rotate_image(regions_without_separators, slope_deskew), text_regions_p, num_col_classifier, slope_deskew, kernel=KERNEL)
            except Exception as e:
                self.logger.error("exception %s", e)

        if self.plotter:
            self.plotter.save_plot_of_layout_main_all(text_regions_p, image_page)
            self.plotter.save_plot_of_layout_main(text_regions_p, image_page)
        return textline_mask_tot, text_regions_p, image_page_rotated

    def run_boxes_no_full_layout(self, image_page, textline_mask_tot, text_regions_p, slope_deskew, num_col_classifier, table_prediction, erosion_hurts):
        self.logger.debug('enter run_boxes_no_full_layout')
        if np.abs(slope_deskew) >= SLOPE_THRESHOLD:
            _, textline_mask_tot_d, text_regions_p_1_n, table_prediction_n = rotation_not_90_func(image_page, textline_mask_tot, text_regions_p, table_prediction, slope_deskew)
            text_regions_p_1_n = resize_image(text_regions_p_1_n, text_regions_p.shape[0], text_regions_p.shape[1])
            textline_mask_tot_d = resize_image(textline_mask_tot_d, text_regions_p.shape[0], text_regions_p.shape[1])
            table_prediction_n = resize_image(table_prediction_n, text_regions_p.shape[0], text_regions_p.shape[1])
            regions_without_separators_d = (text_regions_p_1_n[:, :] == 1) * 1
            if self.tables:
                regions_without_separators_d[table_prediction_n[:,:] == 1] = 1
        regions_without_separators = (text_regions_p[:, :] == 1) * 1  # ( (text_regions_p[:,:]==1) | (text_regions_p[:,:]==2) )*1 #self.return_regions_without_separators_new(text_regions_p[:,:,0],img_only_regions)
        if self.tables:
            regions_without_separators[table_prediction ==1 ] = 1
        if np.abs(slope_deskew) < SLOPE_THRESHOLD:
            text_regions_p_1_n = None
            textline_mask_tot_d = None
            regions_without_separators_d = None
        pixel_lines = 3
        if np.abs(slope_deskew) < SLOPE_THRESHOLD:
            _, _, matrix_of_lines_ch, splitter_y_new, _ = find_number_of_columns_in_document(np.repeat(text_regions_p[:, :, np.newaxis], 3, axis=2), num_col_classifier, self.tables, pixel_lines)

        if np.abs(slope_deskew) >= SLOPE_THRESHOLD:
            _, _, matrix_of_lines_ch_d, splitter_y_new_d, _ = find_number_of_columns_in_document(np.repeat(text_regions_p_1_n[:, :, np.newaxis], 3, axis=2), num_col_classifier, self.tables, pixel_lines)
        K.clear_session()

        self.logger.info("num_col_classifier: %s", num_col_classifier)

        if num_col_classifier >= 3:
            if np.abs(slope_deskew) < SLOPE_THRESHOLD:
                regions_without_separators = regions_without_separators.astype(np.uint8)
                regions_without_separators = cv2.erode(regions_without_separators[:, :], KERNEL, iterations=6)
            else:
                regions_without_separators_d = regions_without_separators_d.astype(np.uint8)
                regions_without_separators_d = cv2.erode(regions_without_separators_d[:, :], KERNEL, iterations=6)
        t1 = time.time()
        if np.abs(slope_deskew) < SLOPE_THRESHOLD:
            boxes, peaks_neg_tot_tables = return_boxes_of_images_by_order_of_reading_new(splitter_y_new, regions_without_separators, matrix_of_lines_ch, num_col_classifier, erosion_hurts, self.tables)
            boxes_d = None
            self.logger.debug("len(boxes): %s", len(boxes))
            
            text_regions_p_tables = np.copy(text_regions_p)
            text_regions_p_tables[:,:][(table_prediction[:,:] == 1)] = 10
            pixel_line = 3
            img_revised_tab2 = self.add_tables_heuristic_to_layout(text_regions_p_tables, boxes, 0, splitter_y_new, peaks_neg_tot_tables, text_regions_p_tables , num_col_classifier , 0.000005, pixel_line)
            img_revised_tab2, contoures_tables = self.check_iou_of_bounding_box_and_contour_for_tables(img_revised_tab2,table_prediction, 10, num_col_classifier)
        else:
            boxes_d, peaks_neg_tot_tables_d = return_boxes_of_images_by_order_of_reading_new(splitter_y_new_d, regions_without_separators_d, matrix_of_lines_ch_d, num_col_classifier, erosion_hurts, self.tables)
            boxes = None
            self.logger.debug("len(boxes): %s", len(boxes_d))
            
            text_regions_p_tables = np.copy(text_regions_p_1_n)
            text_regions_p_tables =np.round(text_regions_p_tables)
            text_regions_p_tables[:,:][(text_regions_p_tables[:,:] != 3) & (table_prediction_n[:,:] == 1)] = 10
            
            pixel_line = 3
            img_revised_tab2 = self.add_tables_heuristic_to_layout(text_regions_p_tables,boxes_d,0,splitter_y_new_d,peaks_neg_tot_tables_d,text_regions_p_tables, num_col_classifier, 0.000005, pixel_line)
            img_revised_tab2_d,_ = self.check_iou_of_bounding_box_and_contour_for_tables(img_revised_tab2,table_prediction_n, 10, num_col_classifier)
            
            img_revised_tab2_d_rotated = rotate_image(img_revised_tab2_d, -slope_deskew)
            img_revised_tab2_d_rotated = np.round(img_revised_tab2_d_rotated)
            img_revised_tab2_d_rotated = img_revised_tab2_d_rotated.astype(np.int8)
            img_revised_tab2_d_rotated = resize_image(img_revised_tab2_d_rotated, text_regions_p.shape[0], text_regions_p.shape[1])

        self.logger.info("detecting boxes took %.1fs", time.time() - t1)
        
        if self.tables:
            if np.abs(slope_deskew) < SLOPE_THRESHOLD:
                img_revised_tab = np.copy(img_revised_tab2[:,:,0])
                img_revised_tab[:,:][(text_regions_p[:,:] == 1) & (img_revised_tab[:,:] != 10)] = 1
            else:
                img_revised_tab = np.copy(text_regions_p[:,:])
                img_revised_tab[:,:][img_revised_tab[:,:] == 10] = 0
                img_revised_tab[:,:][img_revised_tab2_d_rotated[:,:,0] == 10] = 10
                
            text_regions_p[:,:][text_regions_p[:,:]==10] = 0
            text_regions_p[:,:][img_revised_tab[:,:]==10] = 10
        else:
            img_revised_tab=text_regions_p[:,:]
        #img_revised_tab = text_regions_p[:, :]
        polygons_of_images = return_contours_of_interested_region(img_revised_tab, 2)

        pixel_img = 4
        min_area_mar = 0.00001
        polygons_of_marginals = return_contours_of_interested_region(text_regions_p, pixel_img, min_area_mar)
        
        pixel_img = 10
        contours_tables = return_contours_of_interested_region(text_regions_p, pixel_img, min_area_mar)
        
        
        K.clear_session()
        self.logger.debug('exit run_boxes_no_full_layout')
        return polygons_of_images, img_revised_tab, text_regions_p_1_n, textline_mask_tot_d, regions_without_separators_d, boxes, boxes_d, polygons_of_marginals, contours_tables

    def run_boxes_full_layout(self, image_page, textline_mask_tot, text_regions_p, slope_deskew, num_col_classifier, img_only_regions, table_prediction, erosion_hurts):
        self.logger.debug('enter run_boxes_full_layout')
        
        if self.tables:
            if np.abs(slope_deskew) >= SLOPE_THRESHOLD:
                image_page_rotated_n,textline_mask_tot_d,text_regions_p_1_n , table_prediction_n = rotation_not_90_func(image_page, textline_mask_tot, text_regions_p, table_prediction, slope_deskew)
                
                text_regions_p_1_n = resize_image(text_regions_p_1_n,text_regions_p.shape[0],text_regions_p.shape[1])
                textline_mask_tot_d = resize_image(textline_mask_tot_d,text_regions_p.shape[0],text_regions_p.shape[1])
                table_prediction_n = resize_image(table_prediction_n,text_regions_p.shape[0],text_regions_p.shape[1])
                
                regions_without_separators_d=(text_regions_p_1_n[:,:] == 1)*1
                regions_without_separators_d[table_prediction_n[:,:] == 1] = 1
            else:
                text_regions_p_1_n = None
                textline_mask_tot_d = None
                regions_without_separators_d = None
                
            regions_without_separators = (text_regions_p[:,:] == 1)*1#( (text_regions_p[:,:]==1) | (text_regions_p[:,:]==2) )*1 #self.return_regions_without_seperators_new(text_regions_p[:,:,0],img_only_regions)
            regions_without_separators[table_prediction == 1] = 1
            
            pixel_lines=3
            if np.abs(slope_deskew) < SLOPE_THRESHOLD:
                num_col, peaks_neg_fin, matrix_of_lines_ch, splitter_y_new, seperators_closeup_n = find_number_of_columns_in_document(np.repeat(text_regions_p[:, :, np.newaxis], 3, axis=2), num_col_classifier, self.tables, pixel_lines)
            
            if np.abs(slope_deskew) >= SLOPE_THRESHOLD:
                num_col_d, peaks_neg_fin_d, matrix_of_lines_ch_d, splitter_y_new_d, seperators_closeup_n_d = find_number_of_columns_in_document(np.repeat(text_regions_p_1_n[:, :, np.newaxis], 3, axis=2),num_col_classifier, self.tables, pixel_lines)
            K.clear_session()
            gc.collect()

            if num_col_classifier>=3:
                if np.abs(slope_deskew) < SLOPE_THRESHOLD:
                    regions_without_separators = regions_without_separators.astype(np.uint8)
                    regions_without_separators = cv2.erode(regions_without_separators[:,:], KERNEL, iterations=6)
                
                if np.abs(slope_deskew) >= SLOPE_THRESHOLD:
                    regions_without_separators_d = regions_without_separators_d.astype(np.uint8)
                    regions_without_separators_d = cv2.erode(regions_without_separators_d[:,:], KERNEL, iterations=6)
            else:
                pass
            
            if np.abs(slope_deskew) < SLOPE_THRESHOLD:
                boxes, peaks_neg_tot_tables = return_boxes_of_images_by_order_of_reading_new(splitter_y_new, regions_without_separators, matrix_of_lines_ch, num_col_classifier, erosion_hurts, self.tables)
                text_regions_p_tables = np.copy(text_regions_p)
                text_regions_p_tables[:,:][(table_prediction[:,:]==1)] = 10
                pixel_line = 3
                img_revised_tab2 = self.add_tables_heuristic_to_layout(text_regions_p_tables, boxes, 0, splitter_y_new, peaks_neg_tot_tables, text_regions_p_tables , num_col_classifier , 0.000005, pixel_line)
                
                img_revised_tab2,contoures_tables = self.check_iou_of_bounding_box_and_contour_for_tables(img_revised_tab2, table_prediction, 10, num_col_classifier)
                
            else:
                boxes_d, peaks_neg_tot_tables_d = return_boxes_of_images_by_order_of_reading_new(splitter_y_new_d, regions_without_separators_d, matrix_of_lines_ch_d, num_col_classifier, erosion_hurts, self.tables)
                text_regions_p_tables = np.copy(text_regions_p_1_n)
                text_regions_p_tables = np.round(text_regions_p_tables)
                text_regions_p_tables[:,:][(text_regions_p_tables[:,:]!=3) & (table_prediction_n[:,:]==1)] = 10
                
                pixel_line = 3
                img_revised_tab2 = self.add_tables_heuristic_to_layout(text_regions_p_tables,boxes_d,0,splitter_y_new_d,peaks_neg_tot_tables_d,text_regions_p_tables, num_col_classifier, 0.000005, pixel_line)
                
                img_revised_tab2_d,_ = self.check_iou_of_bounding_box_and_contour_for_tables(img_revised_tab2, table_prediction_n, 10, num_col_classifier)
                img_revised_tab2_d_rotated = rotate_image(img_revised_tab2_d, -slope_deskew)
                

                img_revised_tab2_d_rotated = np.round(img_revised_tab2_d_rotated)
                img_revised_tab2_d_rotated = img_revised_tab2_d_rotated.astype(np.int8)

                img_revised_tab2_d_rotated = resize_image(img_revised_tab2_d_rotated, text_regions_p.shape[0], text_regions_p.shape[1])


            if np.abs(slope_deskew) < 0.13:
                img_revised_tab = np.copy(img_revised_tab2[:,:,0])
            else:
                img_revised_tab = np.copy(text_regions_p[:,:])
                img_revised_tab[:,:][img_revised_tab[:,:] == 10] = 0
                img_revised_tab[:,:][img_revised_tab2_d_rotated[:,:,0] == 10] = 10
                    
                    
            ##img_revised_tab=img_revised_tab2[:,:,0]
            #img_revised_tab=text_regions_p[:,:]
            text_regions_p[:,:][text_regions_p[:,:]==10] = 0
            text_regions_p[:,:][img_revised_tab[:,:]==10] = 10
            #img_revised_tab[img_revised_tab2[:,:,0]==10] =10
            
        pixel_img = 4
        min_area_mar = 0.00001
        polygons_of_marginals = return_contours_of_interested_region(text_regions_p, pixel_img, min_area_mar)
        
        pixel_img = 10
        contours_tables = return_contours_of_interested_region(text_regions_p, pixel_img, min_area_mar)
        
        # set first model with second model
        text_regions_p[:, :][text_regions_p[:, :] == 2] = 5
        text_regions_p[:, :][text_regions_p[:, :] == 3] = 6
        text_regions_p[:, :][text_regions_p[:, :] == 4] = 8

        K.clear_session()
        image_page = image_page.astype(np.uint8)

        regions_fully, regions_fully_only_drop = self.extract_text_regions(image_page, True, cols=num_col_classifier)
        text_regions_p[:,:][regions_fully[:,:,0]==6]=6
        regions_fully_only_drop = put_drop_out_from_only_drop_model(regions_fully_only_drop, text_regions_p)
        regions_fully[:, :, 0][regions_fully_only_drop[:, :, 0] == 4] = 4
        K.clear_session()

        # plt.imshow(regions_fully[:,:,0])
        # plt.show()
        regions_fully = putt_bb_of_drop_capitals_of_model_in_patches_in_layout(regions_fully)
        # plt.imshow(regions_fully[:,:,0])
        # plt.show()
        K.clear_session()
        regions_fully_np, _ = self.extract_text_regions(image_page, False, cols=num_col_classifier)
        # plt.imshow(regions_fully_np[:,:,0])
        # plt.show()
        if num_col_classifier > 2:
            regions_fully_np[:, :, 0][regions_fully_np[:, :, 0] == 4] = 0
        else:
            regions_fully_np = filter_small_drop_capitals_from_no_patch_layout(regions_fully_np, text_regions_p)

        # plt.imshow(regions_fully_np[:,:,0])
        # plt.show()
        K.clear_session()
        # plt.imshow(regions_fully[:,:,0])
        # plt.show()
        regions_fully = boosting_headers_by_longshot_region_segmentation(regions_fully, regions_fully_np, img_only_regions)
        # plt.imshow(regions_fully[:,:,0])
        # plt.show()
        text_regions_p[:, :][regions_fully[:, :, 0] == 4] = 4
        text_regions_p[:, :][regions_fully_np[:, :, 0] == 4] = 4
        #plt.imshow(text_regions_p)
        #plt.show()
        ####if not self.tables:
        if np.abs(slope_deskew) >= SLOPE_THRESHOLD:
            _, textline_mask_tot_d, text_regions_p_1_n, regions_fully_n = rotation_not_90_func_full_layout(image_page, textline_mask_tot, text_regions_p, regions_fully, slope_deskew)

            text_regions_p_1_n = resize_image(text_regions_p_1_n, text_regions_p.shape[0], text_regions_p.shape[1])
            textline_mask_tot_d = resize_image(textline_mask_tot_d, text_regions_p.shape[0], text_regions_p.shape[1])
            regions_fully_n = resize_image(regions_fully_n, text_regions_p.shape[0], text_regions_p.shape[1])
            if not self.tables:
                regions_without_separators_d = (text_regions_p_1_n[:, :] == 1) * 1
        else:
            text_regions_p_1_n = None
            textline_mask_tot_d = None
            regions_without_separators_d = None
        if not self.tables:
            regions_without_separators = (text_regions_p[:, :] == 1) * 1

        K.clear_session()
        img_revised_tab = np.copy(text_regions_p[:, :])
        polygons_of_images = return_contours_of_interested_region(img_revised_tab, 5)
        self.logger.debug('exit run_boxes_full_layout')
        return polygons_of_images, img_revised_tab, text_regions_p_1_n, textline_mask_tot_d, regions_without_separators_d, regions_fully, regions_without_separators, polygons_of_marginals, contours_tables
    def write_into_page_xml_hand(self,contours,all_found_texline_polygons,image_dir,height_org,width_org,f_name,image_filename):
        ##found_polygons_text_region_h=contours_h
        found_polygons_text_region=contours

        # create the file structure
        data = ET.Element('PcGts')

        data.set('xmlns',"http://schema.primaresearch.org/PAGE/gts/pagecontent/2017-07-15")
        data.set('xmlns:xsi',"http://www.w3.org/2001/XMLSchema-instance")
        data.set('xsi:schemaLocation',"http://schema.primaresearch.org/PAGE/gts/pagecontent/2017-07-15")



        metadata=ET.SubElement(data,'Metadata')

        author=ET.SubElement(metadata, 'Creator')
        author.text = 'SBB_QURATOR'


        created=ET.SubElement(metadata, 'Created')
        created.text = '2019-06-17T18:15:12'

        changetime=ET.SubElement(metadata, 'LastChange')
        changetime.text = '2019-06-17T18:15:12' 



        page=ET.SubElement(data,'Page')

        page.set('imageFilename', os.path.basename(image_filename))
        page.set('imageHeight',str(height_org))
        page.set('imageWidth',str(width_org))
        page.set('type',"content")
        page.set('readingDirection',"left-to-right")
        page.set('textLineOrder',"top-to-bottom")




        id_indexer=0
        id_indexer_l=0

        for mm in range(len(found_polygons_text_region)):
            textregion=ET.SubElement(page, 'TextRegion')

            textregion.set('id','r'+str(id_indexer))
            id_indexer+=1

            textregion.set('type','paragraph')
            #if mm==0:
            #    textregion.set('type','header')
            #else:
            #    textregion.set('type','paragraph')
            coord_text = ET.SubElement(textregion, 'Coords')

            points_co=''
            for lmm in range(len(found_polygons_text_region[mm])):
                #print(len(found_polygons_text_region[mm][lmm]))
                if len(found_polygons_text_region[mm][lmm])==2:
                    points_co=points_co+str( int( (found_polygons_text_region[mm][lmm][0] ) ) )
                    points_co=points_co+','
                    points_co=points_co+str( int( (found_polygons_text_region[mm][lmm][1] ) ) )
                else:
                    points_co=points_co+str( int((found_polygons_text_region[mm][lmm][0][0]) ) )
                    points_co=points_co+','
                    points_co=points_co+str( int((found_polygons_text_region[mm][lmm][0][1] ) ) )

                if lmm<(len(found_polygons_text_region[mm])-1):
                    points_co=points_co+' '
            #print(points_co)
            coord_text.set('points',points_co)
                    

            for j in range(len(all_found_texline_polygons)):

                textline=ET.SubElement(textregion, 'TextLine')

                textline.set('id','l'+str(id_indexer_l))

                id_indexer_l+=1


                coord = ET.SubElement(textline, 'Coords')

                texteq=ET.SubElement(textline, 'TextEquiv')

                uni=ET.SubElement(texteq, 'Unicode')
                uni.text = ' ' 

                #points = ET.SubElement(coord, 'Points') 

                points_co=''
                for l in range(len(all_found_texline_polygons[j])):
                    #point = ET.SubElement(coord, 'Point') 


                    if len(all_found_texline_polygons[j][l])==2:
                        points_co=points_co+str( int( (all_found_texline_polygons[j][l][0]) ) )
                        points_co=points_co+','
                        points_co=points_co+str( int( (all_found_texline_polygons[j][l][1] ) ) )
                    else:
                        points_co=points_co+str( int( ( all_found_texline_polygons[j][l][0][0] ) ) )
                        points_co=points_co+','
                        points_co=points_co+str( int( ( all_found_texline_polygons[j][l][0][1] ) ) )




                    if l<(len(all_found_texline_polygons[j])-1):
                        points_co=points_co+' '
                #print(points_co)
                coord.set('points',points_co)

        texteqreg=ET.SubElement(textregion, 'TextEquiv')

        unireg=ET.SubElement(texteqreg, 'Unicode')
        unireg.text = ' ' 




        #print(os.path.join(dir_of_image, self.f_name) + ".xml")
        tree = ET.ElementTree(data)
        tree.write(os.path.join(image_dir, f_name) + ".xml")
        #cv2.imwrite(os.path.join(dir_of_image, self.f_name) + ".tif",self.image_org)

    def run(self):
        """
        Get image and scales, then extract the page of scanned image
        """
        self.logger.debug("enter run")

        t0 = time.time()
        
        img = self.imread()
        model_bin, session_bin = self.start_new_session_and_model(self.model_dir_of_binarization)
        prediction_bin = self.do_prediction(True, img, model_bin)
        
        prediction_bin=prediction_bin[:,:,0]
        prediction_bin = (prediction_bin[:,:]==0)*1
        prediction_bin = prediction_bin*255
        
        prediction_bin =np.repeat(prediction_bin[:, :, np.newaxis], 3, axis=2)

        session_bin.close()
        del model_bin
        del session_bin
        gc.collect()
        
        prediction_bin = prediction_bin.astype(np.uint8)
        img= np.copy(prediction_bin)
        img_bin = np.copy(prediction_bin)
        

        model_textline, session_textline = self.start_new_session_and_model(self.model_textline_dir)
        prediction_textline = self.do_prediction(True, img_bin, model_textline, 0.14, True)
        
        #plt.imshow(prediction_textline[:,:,0])
        #plt.show()
        
        #sys.exit()
        session_textline.close()
        del model_textline
        del session_textline
        gc.collect()
        
        contoures_lines = return_contours_of_interested_region_by_min_size(prediction_textline,2,0.00003)
        contoures_limits = return_contours_of_interested_region_by_min_size(prediction_textline,1)
        
        cx_line_early, cy_line_early, x_min_line, x_max_line, y_min_line, y_max_line, _ = find_new_features_of_contours(contoures_lines)
        cx_limits, cy_limits, x_min_limits, x_max_limits, y_min_limits, y_max_limits, _ = find_new_features_of_contours(contoures_limits)
        
        cnt_size_early = np.array([cv2.contourArea(contoures_lines[j]) for j in range(len(contoures_lines))])
        
        all_contoures  = []
        KERNEL = np.ones((5, 5), np.uint8)
        for arg_max in tqdm(range(len(contoures_lines))):
            try:


                ##print(arg_max,'arg_maxarg_maxarg_maxarg_max')

                #contoures_lines_scaled = scale_contour_new(contoures_lines[arg_max],6,label)
                #contoures_lines_scaled2 = scale_contour_new(contoures_lines[arg_max],2,label)
                img_con_early  = np.zeros((prediction_textline.shape[0],prediction_textline.shape[1]))
                img_con_early =  cv2.fillPoly(img_con_early, pts=[contoures_lines[arg_max]], color=(1, 1))

                #img_con_early_scaled  = np.zeros((label.shape[0],label.shape[1]))
                #mask =  cv2.fillPoly(img_con_early_scaled, pts=[contoures_lines_scaled], color=(1, 1))

                #img_con_early_scaled2  = np.zeros((label.shape[0],label.shape[1]))
                #mask18 =  cv2.fillPoly(img_con_early_scaled2, pts=[contoures_lines_scaled2], color=(1, 1))

                #plt.figure(figsize=(20,20))
                #plt.imshow(img_con_early)
                #plt.show()

                img_con_early_sum = img_con_early.sum(axis=0)

                #plt.plot(img_con_early_sum)
                #plt.show()

                args_non_zero = np.array(range(len(img_con_early_sum)))[img_con_early_sum!=0]
                #print(args_non_zero)
                x_min = min(args_non_zero)
                x_max = max(args_non_zero)

                mean_width = np.mean(img_con_early_sum[args_non_zero])
                #print(mean_width,'meanwidth')
                #print(x_min,x_max)
                #plt.plot(img_con_early_sum)
                #plt.show()



                #plt.figure(figsize=(20,20))
                #plt.imshow(mask)
                #plt.show()

                #plt.figure(figsize=(20,20))
                #plt.imshow(mask18)
                #plt.show()


                index_dil = 1.
                dilation_early = int(18/index_dil)

                mask18 = cv2.dilate(img_con_early, KERNEL, iterations=dilation_early)


                mask18_sum = mask18.sum(axis=0)



                args_non_zero_mask18 = np.array(range(len(mask18_sum)))[mask18_sum!=0]

                meanwidth18 = np.mean(mask18_sum[args_non_zero_mask18])

                #print(meanwidth18,'meanwidth18')
                #print(meanwidth18/mean_width,'ratio')


                if meanwidth18/mean_width>4.5:
                    index_dil = 2.
                    dilation_early = int(18/index_dil)

                    mask18 = cv2.dilate(img_con_early, KERNEL, iterations=dilation_early)



                dilation_sec = int(2/index_dil)
                dilation_third = int(10/index_dil)#int(20/index_dil)

                mask20 = cv2.dilate(mask18, KERNEL, iterations=dilation_sec)


                mask_bound = mask20 -mask18
                mask = cv2.dilate(mask20, KERNEL, iterations=dilation_third)


                #mask20 = cv2.dilate(mask18, KERNEL, iterations=2)

                #mask_bound = mask20 -mask18


                mask_xmin_xmax = np.zeros(mask.shape)
                mask_xmin_xmax[:,x_min:x_max][mask[:,x_min:x_max]==1] =1


                mask_upper_and_lower = mask_xmin_xmax - img_con_early

                labe_limits_around_line = np.copy(prediction_textline[:,:,0])
                labe_limits_around_line[mask!=1]=0
                #print(np.unique(mask))
                #plt.figure(figsize=(20,20))
                #plt.imshow(mask)
                #plt.show()

                #plt.figure(figsize=(20,20))
                #plt.imshow(mask_bound)
                #plt.show()

                #plt.figure(figsize=(20,20))
                #plt.imshow(mask_xmin_xmax)
                #plt.show()
                #plt.figure(figsize=(20,20))
                #plt.imshow(mask20)
                #plt.show()

                #plt.figure(figsize=(20,20))
                #plt.imshow(labe_limits_around_line)
                #plt.show()

                mask_upper_and_lower[mask_upper_and_lower<0]=0
                #print(np.unique(mask_upper_and_lower))
                #plt.figure(figsize=(20,20))
                #plt.imshow(mask_upper_and_lower)
                #plt.show()

                con_fin,hir_fin = return_contours_of_image(mask_upper_and_lower)

                #print(len(con_fin))



                cx_line, cy_line, _, _, _, _, _ = find_new_features_of_contours(con_fin)

                if len(con_fin)>2:
                    cnt_size_fin_e = np.array([cv2.contourArea(con_fin[j]) for j in range(len(con_fin))])
                    arg_sort = np.argsort(cnt_size_fin_e)
                    arg_sort=arg_sort[::-1]

                    con_fin =np.array(con_fin)[arg_sort][:2]
                    cy_line = np.array(cy_line)[arg_sort][:2]
                #print(cx_line,'cx_linecx_linecx_line')
                if cy_line[0]>cy_line[1]:
                    arg_upper = 1
                    arg_lower = 0
                else:
                    arg_upper = 0
                    arg_lower = 1

                upper_region  = np.zeros((prediction_textline.shape[0],prediction_textline.shape[1]))
                upper_region =  cv2.fillPoly(upper_region, pts=[con_fin[arg_upper]], color=(1, 1)) 

                lower_region  = np.zeros((prediction_textline.shape[0],prediction_textline.shape[1]))
                lower_region =  cv2.fillPoly(lower_region, pts=[con_fin[arg_lower]], color=(1, 1)) 


                #plt.figure(figsize=(20,20))
                #plt.imshow(upper_region)
                #plt.show()

                #plt.figure(figsize=(20,20))
                #plt.imshow(lower_region)
                #plt.show()


                labe_limits_around_line_upper = np.copy(prediction_textline[:,:,0])
                labe_limits_around_line_upper[upper_region!=1]=0

                labe_limits_around_line_bound_upper = np.copy(mask_bound[:,:])
                labe_limits_around_line_bound_upper[upper_region!=1]=0


                labe_limits_around_line_lower = np.copy(prediction_textline[:,:,0])
                labe_limits_around_line_lower[lower_region!=1]=0

                labe_limits_around_line_bound_lower = np.copy(mask_bound[:,:])
                labe_limits_around_line_bound_lower[lower_region!=1]=0


                #plt.figure(figsize=(20,20))
                #plt.imshow(labe_limits_around_line_upper)
                #plt.show()

                #plt.figure(figsize=(20,20))
                #plt.imshow( (labe_limits_around_line_upper[:,:]==1)*1)
                #plt.show()

                #plt.figure(figsize=(20,20))
                #plt.imshow(labe_limits_around_line_bound_upper)
                #plt.show()


                #plt.figure(figsize=(20,20))
                #plt.imshow(labe_limits_around_line_lower)
                #plt.show()


                #plt.figure(figsize=(20,20))
                #plt.imshow(labe_limits_around_line_bound_lower)
                #plt.show()
                #plt.figure(figsize=(20,20))
                #plt.imshow(labe_limits_around_line_bound_lower)
                #plt.show()


                #test_image = np.zeros(labe_limits_around_line_bound_lower.shape)

                #indexes_test = [1,2,3,4,5,6,7,8,9,100,101,102,103,104,200,201,202,203,500,501,502,503,504,505,506,507,508,509,510]

                #print(np.unique(labe_limits_around_line_bound_lower))
                #test_image[:,np.array(indexes_test)]= labe_limits_around_line_bound_lower[:,np.array(indexes_test)]

                #plt.figure(figsize=(20,20))
                #plt.imshow(test_image)
                #plt.show()
                
                #if arg_max == 37:
                    #plt.imshow(labe_limits_around_line_lower)
                    #plt.show()
                modified_lower = self.return_modified_lower_limits_image_new(labe_limits_around_line_lower,labe_limits_around_line_bound_lower)


                #if arg_max == 37:
                    #plt.imshow(modified_lower)
                    #plt.show()
                    
                modified_lower[lower_region==0] =0
                
                #if arg_max == 37:
                    
                    #plt.imshow(lower_region)
                    #plt.show()
                    
                    
                    #plt.imshow(modified_lower)
                    #plt.show()


                #plt.figure(figsize=(20,20))
                ##plt.imshow(labe_limits_around_line_upper)
                ##plt.show()
                modified_upper = self.return_modified_upper_limits_image_new(labe_limits_around_line_upper,labe_limits_around_line_bound_upper)

                
                #plt.figure(figsize=(20,20))
                #plt.imshow(modified_upper)
                #plt.show()
                modified_upper[upper_region==0] =0


                modified_upper[modified_lower==1]=1
                #print(x_min-5,'x_min-5x_min-5x_min-5')
                x_min_start = x_min-3
                x_min_end = x_min+3
                
                x_max_start = x_max-3
                x_max_end = x_max+3
                
                
                if x_min_start<0:
                    x_min_start = 0
                modified_upper[:,x_min_start:x_min_end]=1
                modified_upper[:,x_max_start:x_max_end]=1

                modified_upper[mask==0] = 0

                modified_upper =modified_upper.astype('uint16')
                KERNEL = np.ones((5, 5), np.uint8)

                modified_upper = cv2.dilate(modified_upper, KERNEL, iterations=1)
                modified_upper = cv2.erode(modified_upper, KERNEL, iterations=1)
                
                modified_upper_upper_three = modified_upper[0:3,:]
                modified_upper_upper_three_sum = modified_upper_upper_three.sum(axis = 0)
                
                index_upper_3_non_zero = np.array(range(len(modified_upper_upper_three_sum)))[np.array(modified_upper_upper_three_sum) !=0 ]
                
                if len(index_upper_3_non_zero)>=1:
                    modified_upper[0:1,min(index_upper_3_non_zero):max(index_upper_3_non_zero)] = 1
                #print(index_upper_3_non_zero,'modified_upper_upper_three_summodified_upper_upper_three_sum')
                
                
                modified_upper_lower_three = modified_upper[modified_upper.shape[0]-3:modified_upper.shape[0],:]
                modified_upper_lower_three_sum = modified_upper_lower_three.sum(axis = 0)
                
                index_lower_3_non_zero = np.array(range(len(modified_upper_lower_three_sum)))[np.array(modified_upper_lower_three_sum) !=0 ]
                
                if len(index_lower_3_non_zero)>=1:
                    modified_upper[modified_upper.shape[0]-1:modified_upper.shape[0],min(index_lower_3_non_zero):max(index_lower_3_non_zero)] = 1
                #modified_upper[0:1,:] = 1
                #modified_upper[modified_upper.shape[0]-1:modified_upper.shape[0],:] = 1
                
                
                
                modi = np.copy(modified_upper)
                modi[labe_limits_around_line_bound_upper==1]=3
                #plt.figure(figsize=(20,20))
                #if arg_max == 37:
                #plt.imshow(modified_upper)
                    ####plt.imshow(labe_limits_around_line_bound_upper)
                #plt.show()


                con_fin,hir_fin = return_contours_of_image(modified_upper)
                #print(len(con_fin),'early')
                #con_fin = filter_contours_area_of_image(modified_upper, con_fin, hir_fin, max_area=1, min_area=0.00000)
                #con_fin = filter_contours_area_of_image_tables(modified_upper, con_fin, _, max_area = 1, min_area = 0.005)
                #print(len(con_fin),'secondary')
                cnt_size_fin_1 = np.array([cv2.contourArea(con_fin[j]) for j in range(len(con_fin))])
                
                ##distance_size = (np.array(cnt_size_fin_1) - cnt_size_early[arg_max])**2
                
                arg_size_sorted = np.argsort(cnt_size_fin_1)
                arg_size_sorted =arg_size_sorted[::-1]
                
                #print(distance_size,'distance_sizedistance_sizedistance_size')
                
                ##con_fin = list(np.array(con_fin)[arg_size_sorted[:3]])
                con_fin = list(np.array(con_fin)[arg_size_sorted[:2]])
                #print(len(con_fin),'con_fincon_fincon_fincon_fin')
                
                cx_line_fin, cy_line_fin, _, _, _, _, _ = find_new_features_of_contours(con_fin)
                
                distance = (np.array(cx_line_fin)-cx_line_early[arg_max])**2 + (np.array(cy_line_fin)-cy_line_early[arg_max])**2
                
                arg_distnace_sorted = np.argsort(distance)
                #print(distance[arg_distnace_sorted],'arg_distnace_sortedarg_distnace_sortedarg_distnace_sorted')
                arg_distance_min = np.argmin(distance)
                
                #print(arg_distance_min,'arg_distance_min')
                
                
                #print(cx_line_early[arg_max], cy_line_early[arg_max], 'cx_line_early, cy_line_early')
                #print(cx_line_fin, cy_line_fin, 'cx_line_early, cy_line_early')
                
                con_fin_2_nearest = list(np.array(con_fin)[arg_distnace_sorted[:2]] )
                
                cnt_size_fin = np.array([cv2.contourArea(con_fin_2_nearest[j]) for j in range(len(con_fin_2_nearest))])
                #####cnt_size_fin = np.array([cv2.contourArea(con_fin[j]) for j in range(len(con_fin))])
                #con_fin = [con_fin[j] for j in range(len(con_fin)) if hir_fin[0][j][3]!=-1]  
                ###cnt_size_fin = np.array([cv2.contourArea(con_fin[j]) for j in range(len(con_fin))])
                ###print(hir_fin,hir_fin[0][0],'hir_finhir_finhir_fin')
                arg_sort = np.argsort(cnt_size_fin)
                arg_sort=arg_sort[::-1]
                
                #print(arg_sort,'arg_sort')

                #img_con_fin_rec  = np.zeros((label.shape[0],label.shape[1]))
                #img_con_fin_rec =  cv2.fillPoly(img_con_fin_rec, pts=con_fin[0], color=(1, 1))

                #plt.figure(figsize=(20,20))
                #plt.imshow(img_con_fin)
                #plt.show()

                """
                for iv in range(len(arg_sort)):
                    img_con_fin  = np.zeros((label.shape[0],label.shape[1]))
                    img_con_fin =  cv2.fillPoly(img_con_fin, pts=[con_fin[arg_sort[iv]]], color=(1, 1))

                    plt.imshow(img_con_fin)
                    plt.show()
                """

                img_con_fin  = np.zeros((prediction_textline.shape[0],prediction_textline.shape[1]))

                #try:
                ##img_con_fin =  cv2.fillPoly(img_con_fin, pts=[con_fin[arg_distance_min]], color=(1, 1))
                img_con_fin =  cv2.fillPoly(img_con_fin, pts=[con_fin_2_nearest[arg_sort[1]]], color=(1, 1))
                ####if len(arg_sort)>1:
                    ####img_con_fin =  cv2.fillPoly(img_con_fin, pts=[con_fin[arg_sort[1]]], color=(1, 1))
                    ####print(cx_line_fin[arg_sort[1]], cy_line_fin[arg_sort[1]],'lenarg_sort')
                ####else:            
                    ####img_con_fin =  cv2.fillPoly(img_con_fin, pts=[con_fin[arg_sort[0]]], color=(1, 1))
                    ####print(cx_line_fin[arg_sort[0]], cy_line_fin[arg_sort[0]],'lenarg_sort')
                    
                img_con_fin = cv2.dilate(img_con_fin, KERNEL, iterations=3)

                #plt.imshow(img_con_fin)
                #plt.show()

                con_fin_textline=return_contours_of_interested_region_by_min_size(img_con_fin,1)
                #print(len(con_fin_textline),'lencon_fin_textline')

                all_contoures.append(con_fin_textline[0])
                #except:
                    #pass
            except:
                pass

        height_org =prediction_textline.shape[0]
        width_org =prediction_textline.shape[1]
        #f_name = 'elabela'
        
    
        img_page_prediction = np.zeros((prediction_textline.shape[0],prediction_textline.shape[1]))
        img_page_prediction[1:prediction_textline.shape[0]-2,1:prediction_textline.shape[1]-2]=1
        
        cnt_page  = return_contours_of_image(img_page_prediction)
        print(self.image_filename_stem)
        self.write_into_page_xml_hand(cnt_page[0],all_contoures,self.dir_out,height_org,width_org,self.image_filename_stem,self.image_filename)

        pcgts = None
        self.logger.info("Job done in %.1fs", time.time() - t0)
        return pcgts
