"""
This class the YOLOv7 QR Detector. It uses a YOLOv7-tiny model trained to detect QR codes in the wild.

Author: Eric Canas.
Github: https://github.com/Eric-Canas/qrdet
Email: eric@ericcanas.com
Date: 11-12-2022
"""

from __future__ import annotations
import os

import numpy as np
import requests
import tqdm


from ultralytics import YOLO

import qrdet
from qrdet import _yolo_v8_results_to_dict, _prepare_input, BBOX_XYXY, CONFIDENCE

import onnxruntime as ort
import cv2 as cv
from PIL import Image, ImageDraw
import time

from qrdet import BBOX_XYXY, BBOX_XYXYN, POLYGON_XY, POLYGON_XYN, \
    CXCY, CXCYN, WH, WHN, IMAGE_SHAPE, CONFIDENCE, PADDED_QUAD_XY, PADDED_QUAD_XYN, \
    QUAD_XY, QUAD_XYN

from quadrilateral_fitter import QuadrilateralFitter


_WEIGHTS_FOLDER = os.path.join(os.path.dirname(__file__), '.model')
_CURRENT_RELEASE_TXT_FILE = os.path.join(_WEIGHTS_FOLDER, 'current_release.txt')
_WEIGHTS_URL_FOLDER = 'https://github.com/Eric-Canas/qrdet/releases/download/v2.0_release'
_MODEL_FILE_NAME = 'qrdet-{size}.pt'


# #############################################################
class QRDetector:
    def __init__(self, model_size: str = 's', conf_th: float = 0.5, nms_iou: float = 0.3):
        """
        Инициализация QRDetector

        :param model_size: str. The size of the model to use. It can be 'n' (nano), 's' (small), 'm' (medium) or
                                'l' (large). Larger models are more accurate but slower. Default (and recommended): 's'.
        :param conf_th: float. The confidence threshold to use for the detections. Detection with a confidence lower
                                than this value will be discarded. Default: 0.5.
        :param nms_iou: float. The IoU threshold to use for the Non-Maximum Suppression. Detections with an IoU higher
                                than this value will be discarded. Default: 0.3.
        """
        assert model_size in ('n', 's', 'm', 'l'), f'Invalid model size: {model_size}. ' \
                                                   f'Valid values are: \'n\', \'s\', \'m\' or \'l\'.'
        self._model_size = model_size
        path = f'models/qrdet-{self._model_size}.onnx'
        assert os.path.exists(path), f'Could not find model weights at {path}.'

        #
        # EP_list = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        EP_list = ['CPUExecutionProvider']
        self.model = ort.InferenceSession(path, providers=EP_list)

        self._conf_th = conf_th
        self._nms_iou = nms_iou

    def detect(self, image: np.ndarray|'PIL.Image'|'torch.Tensor'|str, is_bgr: bool = False,
               **kwargs) -> tuple[dict[str, np.ndarray|float|tuple[float, float]]]:
        """
        Detect QR codes in the given image.

        :param image: str|np.ndarray|PIL.Image|torch.Tensor. Numpy array (H, W, 3), Tensor (1, 3, H, W), or
                                            path/url to the image to predict. 'screen' for grabbing a screenshot.
        :param is_bgr: input image in BGR format
        :return: tuple[dict[str, np.ndarray|float|tuple[float, float]]]. A tuple of dictionaries containing the
            following keys:
            - 'confidence': float. The confidence of the detection.
            - 'bbox_xyxy': np.ndarray. The bounding box of the detection in the format [x1, y1, x2, y2].
            - 'cxcy': tuple[float, float]. The center of the bounding box in the format (x, y).
            - 'wh': tuple[float, float]. The width and height of the bounding box in the format (w, h).
            - 'polygon_xy': np.ndarray. The accurate polygon that surrounds the QR code, with shape (N, 2).
            - 'quadrilateral_xy': np.ndarray. The quadrilateral that surrounds the QR code, with shape (4, 2).
            - 'expanded_quadrilateral_xy': np.ndarray. An expanded version of quadrilateral_xy, with shape (4, 2),
                that always include all the points within polygon_xy.

            All these keys (except 'confidence') have a 'n' (normalized) version. For example, 'bbox_xyxy' is the
            bounding box in absolute coordinates, while 'bbox_xyxyn' is the bounding box in normalized coordinates
            (from 0. to 1.).
        """

        # Любое изображение приводится к numpy
        img = _prepare_input(source=image, is_bgr=is_bgr)
        img_height, img_width = img.shape[:2]

        # Convert the image to tensor of [1,3,640,640]
        input = cv.resize(img, (640, 640), interpolation=cv.INTER_LINEAR)
        input = input.transpose(2, 0, 1)
        input = input.reshape(1, 3, 640, 640).astype('float32')
        input = input / 255.0

        # Predict
        start_time = time.time()
        outputs = self.model.run(None, {"images": input})
        print("Pred--- %s seconds ---" % (time.time() - start_time))

        # start_time = time.time()
        # results = qrdet.process_output(outputs, img_width, img_height)
        # print("--- %s seconds ---" % (time.time() - start_time))

        # output0 = outputs[0]
        # output1 = outputs[1]
        # print("Output0:", output0.shape, "Output1:", output1.shape)

        #
        output0 = outputs[0].astype("float")
        output1 = outputs[1].astype("float")
        output0 = output0[0].transpose()
        output1 = output1[0]
        # boxes = output0[:, 0:84]
        # masks = output0[:, 84:]
        boxes = output0[:, 0:5]  # need to use the (number of classes)+4 to split output to boxes and masks, not 84
        masks = output0[:, 5:]

        # print("Boxes:", boxes.shape, "Masks:", masks.shape)
        output1 = output1.reshape(32, 160 * 160)
        #

        # masks = masks @ output1
        # masks = np.einsum('ij,jk -> ik', masks, output1)
        start_time = time.time()
        # masks = np.dot(masks, output1)
        masks = qrdet.matmult(masks, output1)
        print("post--- %s seconds ---" % (time.time() - start_time))
        boxes = np.hstack((boxes, masks))

        # parse and filter detected objects

        objects = []
        for row in boxes:
            prob = row[4:5].max()  # 84
            if prob < self._conf_th:
                continue
            class_id = row[4:5].argmax()  # 84
            # label = yolo_classes[class_id]
            label = qrdet.custom_classes[class_id]
            #
            xc, yc, w, h = row[:4]
            x1 = (xc - w / 2) / 640 * img_width
            y1 = (yc - h / 2) / 640 * img_height
            x2 = (xc + w / 2) / 640 * img_width
            y2 = (yc + h / 2) / 640 * img_height
            mask = qrdet.get_mask(row[5:25684], (x1, y1, x2, y2), img_width, img_height)  # 84
            polygon = qrdet.get_polygon(mask)
            objects.append([x1, y1, x2, y2, label, prob, polygon])

        objects.sort(key=lambda x: x[5], reverse=True)
        # print(len(objects))

        results = []
        while len(objects) > 0:
            results.append(objects[0])
            objects = [object for object in objects if qrdet.iou(object, objects[0]) < self._nms_iou]
        # print(len(results))

        #
        if len(results) == 0:
            return []

        im_h, im_w = img_height, img_width
        detections = []
        #
        for result in results:
            # print(result)
            x1, y1, x2, y2 = result[:4]
            confidence = result[5]
            bbox_xyxy = np.array([x1, y1, x2, y2])
            bbox_xyxyn = np.array([x1 / im_w, y1 / im_h, x2 / im_w, y2 / im_h])
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            cxn, cyn = cx / im_w, cy / im_h
            bbox_w, bbox_h = x2 - x1, y2 - y1
            bbox_wn, bbox_hn = bbox_w / im_w, bbox_h / im_h

            #
            polygon = result[6]
            accurate_polygon_xyn = []
            for point in polygon:
                # print(point)
                point[0] = point[0] + x1
                point[1] = point[1] + y1
                #
                accurate_polygon_xyn.append([point[0] / im_w, point[1] / im_h])
            #
            accurate_polygon_xy = np.array(polygon)
            accurate_polygon_xyn = np.array(accurate_polygon_xyn)
            # print("polygon", accurate_polygon_xy.shape, accurate_polygon_xy)
            # print("polygon", accurate_polygon_xyn.shape, accurate_polygon_xyn)

            # Fit a quadrilateral to the polygon (Don't clip accurate_polygon_xy yet, to fit the quadrilateral before)
            _quadrilateral_fit = QuadrilateralFitter(polygon=accurate_polygon_xy)
            quadrilateral_xy = _quadrilateral_fit.fit(simplify_polygons_larger_than=8,
                                                      start_simplification_epsilon=0.1,
                                                      max_simplification_epsilon=2.,
                                                      simplification_epsilon_increment=0.2)

            # Clip the data to make sure it's inside the image
            np.clip(bbox_xyxy[::2], a_min=0., a_max=im_w, out=bbox_xyxy[::2])
            np.clip(bbox_xyxy[1::2], a_min=0., a_max=im_h, out=bbox_xyxy[1::2])
            np.clip(bbox_xyxyn, a_min=0., a_max=1., out=bbox_xyxyn)

            np.clip(accurate_polygon_xy[:, 0], a_min=0., a_max=im_w, out=accurate_polygon_xy[:, 0])
            np.clip(accurate_polygon_xy[:, 1], a_min=0., a_max=im_h, out=accurate_polygon_xy[:, 1])
            np.clip(accurate_polygon_xyn, a_min=0., a_max=1., out=accurate_polygon_xyn)

            # NOTE: We are not clipping the quadrilateral to the image size, because we actively want it to be larger
            # than the polygon. It allows cropped QRs to be fully covered by the quadrilateral with only 4 points.

            expanded_quadrilateral_xy = np.array(_quadrilateral_fit.expanded_quadrilateral, dtype=np.float32)
            quadrilateral_xy = np.array(quadrilateral_xy, dtype=np.float32)

            expanded_quadrilateral_xyn = expanded_quadrilateral_xy / (im_w, im_h)
            quadrilateral_xyn = quadrilateral_xy / (im_w, im_h)

            #
            detections.append({
                CONFIDENCE: confidence,

                BBOX_XYXY: bbox_xyxy,
                BBOX_XYXYN: bbox_xyxyn,
                CXCY: (cx, cy), CXCYN: (cxn, cyn),
                WH: (bbox_w, bbox_h), WHN: (bbox_wn, bbox_hn),

                POLYGON_XY: accurate_polygon_xy,
                POLYGON_XYN: accurate_polygon_xyn,
                QUAD_XY: quadrilateral_xy,
                QUAD_XYN: quadrilateral_xyn,
                PADDED_QUAD_XY: expanded_quadrilateral_xy,
                PADDED_QUAD_XYN: expanded_quadrilateral_xyn,

                IMAGE_SHAPE: (im_h, im_w),
            })
            # print(detections[-1]['polygon_xy'])
        qrdet.crop_qr(image=image, detection=detections[0], crop_key=PADDED_QUAD_XYN)

        return detections


# #############################################################
class QRDetectorPT:
    def __init__(self, model_size: str = 's', conf_th: float = 0.5, nms_iou: float = 0.3):
        """
        Initialize the QRDetector.
        It loads the weights of the YOLOv8 model and prepares it for inference.
        :param model_size: str. The size of the model to use. It can be 'n' (nano), 's' (small), 'm' (medium) or
                                'l' (large). Larger models are more accurate but slower. Default (and recommended): 's'.
        :param conf_th: float. The confidence threshold to use for the detections. Detection with a confidence lower
                                than this value will be discarded. Default: 0.5.
        :param nms_iou: float. The IoU threshold to use for the Non-Maximum Suppression. Detections with an IoU higher
                                than this value will be discarded. Default: 0.3.
        """
        assert model_size in ('n', 's', 'm', 'l'), f'Invalid model size: {model_size}. ' \
                                                   f'Valid values are: \'n\', \'s\', \'m\' or \'l\'.'
        self._model_size = model_size
        path = self.__download_weights_or_return_path(model_size=model_size)
        assert os.path.exists(path), f'Could not find model weights at {path}.'

        self.model = YOLO(model=path, task='segment')

        self._conf_th = conf_th
        self._nms_iou = nms_iou

    def detect(self, image: np.ndarray|'PIL.Image'|'torch.Tensor'|str, is_bgr: bool = False,
               **kwargs) -> tuple[dict[str, np.ndarray|float|tuple[float, float]]]:
        """
        Detect QR codes in the given image.

        :param image: str|np.ndarray|PIL.Image|torch.Tensor. Numpy array (H, W, 3), Tensor (1, 3, H, W), or
                                            path/url to the image to predict. 'screen' for grabbing a screenshot.
        :param legacy: bool. If sent as **kwarg**, will parse the output to make it identical to 1.x versions.
                            Not Recommended. Default: False.
        :return: tuple[dict[str, np.ndarray|float|tuple[float, float]]]. A tuple of dictionaries containing the
            following keys:
            - 'confidence': float. The confidence of the detection.
            - 'bbox_xyxy': np.ndarray. The bounding box of the detection in the format [x1, y1, x2, y2].
            - 'cxcy': tuple[float, float]. The center of the bounding box in the format (x, y).
            - 'wh': tuple[float, float]. The width and height of the bounding box in the format (w, h).
            - 'polygon_xy': np.ndarray. The accurate polygon that surrounds the QR code, with shape (N, 2).
            - 'quadrilateral_xy': np.ndarray. The quadrilateral that surrounds the QR code, with shape (4, 2).
            - 'expanded_quadrilateral_xy': np.ndarray. An expanded version of quadrilateral_xy, with shape (4, 2),
                that always include all the points within polygon_xy.

            All these keys (except 'confidence') have a 'n' (normalized) version. For example, 'bbox_xyxy' is the
            bounding box in absolute coordinates, while 'bbox_xyxyn' is the bounding box in normalized coordinates
            (from 0. to 1.).
        """
        image = _prepare_input(source=image, is_bgr=is_bgr)
        # Predict
        results = self.model.predict(source=image, conf=self._conf_th, iou=self._nms_iou, half=False,
                                device=None, max_det=100, augment=False, agnostic_nms=True,
                                classes=None, verbose=False)
        assert len(results) == 1, f'Expected 1 result if no batch sent, got {len(results)}'
        results = _yolo_v8_results_to_dict(results=results[0], image=image)

        if 'legacy' in kwargs and kwargs['legacy']:
            return self._parse_legacy_results(results=results, **kwargs)
        return results

    def _parse_legacy_results(self, results, return_confidences: bool = True, **kwargs) \
            -> tuple[tuple[list[float, float, float, float], float], ...] | tuple[list[float, float, float, float], ...]:
        """
        Parse the results to make it compatible with the legacy version of the library.
        :param results: tuple[dict[str, np.ndarray|float|tuple[float, float]]]. The results to parse.
        """
        if return_confidences:
            return tuple((result[BBOX_XYXY], result[CONFIDENCE]) for result in results)
        else:
            return tuple(result[BBOX_XYXY] for result in results)

    def __download_weights_or_return_path(self, model_size: str = 's', desc: str = 'Downloading weights...') -> None:
        """
        Download the weights of the YoloV8 QR Segmentation model.
        :param model_size: str. The size of the model to download. Can be 's', 'm' or 'l'. Default: 's'.
        :param desc: str. The description of the download. Default: 'Downloading weights...'.
        """
        self.downloading_model = True
        path = os.path.join(_WEIGHTS_FOLDER, _MODEL_FILE_NAME.format(size=model_size))
        if os.path.isfile(path):
            if os.path.isfile(_CURRENT_RELEASE_TXT_FILE):
                # Compare the current release with the actual release URL
                with open(_CURRENT_RELEASE_TXT_FILE, 'r') as file:
                    current_release = file.read()
                # If the current release is the same as the URL, the weights are already downloaded.
                if current_release == _WEIGHTS_URL_FOLDER:
                    self.downloading_model = False
                    return path
        # Create the directory to save the weights.
        elif not os.path.exists(_WEIGHTS_FOLDER):
            os.makedirs(_WEIGHTS_FOLDER)

        url = f"{_WEIGHTS_URL_FOLDER}/{_MODEL_FILE_NAME.format(size=model_size)}"

        # Download the weights.
        from warnings import warn
        warn("QRDetector has been updated to use the new YoloV8 model. Use legacy=True when calling detect "
             "for backwards compatibility with 1.x versions. Or update to new output (new output is a tuple of dicts, "
             "containing several new information (1.x output is accessible through 'bbox_xyxy' and 'confidence')."
             "Forget this message if you are reading it from QReader. "
             "[This is a first download warning and will be removed at 2.1]")
        response = requests.get(url, stream=True)
        total_size_in_bytes = int(response.headers.get('content-length', 0))
        with tqdm.tqdm(total=total_size_in_bytes, unit='iB', unit_scale=True, desc=desc) as progress_bar:
            with open(path, 'wb') as file:
                for data in response.iter_content(chunk_size=1024):
                    progress_bar.update(len(data))
                    file.write(data)
        # Save the current release URL
        with open(_CURRENT_RELEASE_TXT_FILE, 'w') as file:
            file.write(_WEIGHTS_URL_FOLDER)
        # Check the weights were downloaded correctly.
        if total_size_in_bytes != 0 and progress_bar.n != total_size_in_bytes:
            # Delete the weights if the download failed.
            os.remove(path)
            raise EOFError('Error, something went wrong while downloading the weights.')

        self.downloading_model = False
        return path

    def __del__(self):
        path = os.path.join(_WEIGHTS_FOLDER, _MODEL_FILE_NAME.format(size=self._model_size))
        # If the weights didn't finish downloading, delete them.
        if hasattr(self, 'downloading_model') and self.downloading_model and os.path.isfile(path):
            os.remove(path)