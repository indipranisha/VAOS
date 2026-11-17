# Models, datasets and ML training

VAOS separates object detection from face matching. Object detection uses an optional Ultralytics YOLO runtime and local checkpoint files. Face detection/matching uses OpenCV Zoo YuNet and SFace. Do not treat a detector checkpoint as a face-recognition model or vice versa.

## Included custom detector

The source release includes the active `best.pt` checkpoint and matching `metadata.json` under `data/ml_training/active/`. It was trained on 119,070 COCO 2017 and human-reviewed samples, starting from the existing custom checkpoint. Recorded final metrics are precision 0.6041, recall 0.5004, mAP50 0.5359, and mAP50–95 0.3846. These are the saved run metrics and do not guarantee the same accuracy on a different camera or dataset. See [`MODEL_NOTICES.md`](../data/ml_training/active/MODEL_NOTICES.md) for the checkpoint hash and licensing/provenance notes.

VAOS and this bundled Ultralytics-trained model are distributed under GNU AGPL-3.0-only. The repository does not include the COCO or reviewed training images. Their original rights and terms remain separate and must be checked before reuse or redistribution. Do not add private or personally identifiable reviewed images to the repository.

## Dataset workflow

1. Use **Datasets** to inspect, import or prepare a dataset supported by the source build.
2. Preparation validates the folder layout and annotation format and creates a training-ready copy where needed. A downloaded dataset is not automatically detector-ready; segmentation-only annotations, incomplete label maps or missing split files can prevent training.
3. Use **ML Training → Review detections** to inspect and correct reviewed examples. Confirmed samples are separate from external dataset annotations.
4. Choose the desired dataset combination in the training form and confirm the summary before launch. The training activity should record the selected dataset names; verify that record after launch.
5. Keep sufficient disk for the original, prepared copy, caches and run outputs. Purging a prepared copy may remove it from the selectable list; check whether it is the original or derived folder before confirming deletion.

Only use datasets whose license and terms allow the intended use. Keep original dataset citations and license files. VAOS does not grant rights to third-party images, labels or checkpoints.

## Training operations

Select a starting checkpoint, validated training data, epoch count, image size and available compute target in **ML Training**. Available device choice depends on the installed PyTorch runtime and system. CPU is the fallback; CUDA/MPS paths are conditional. Increase epochs only when the dataset, validation split and compute budget justify it. More epochs do not guarantee better generalization and can increase overfitting.

Do not start concurrent training sessions against the same working output unless the application explicitly supports it. Preserve the existing best checkpoint before installing a replacement. Monitor the live job, per-epoch log and final validation. A process being alive does not prove that epochs are advancing.

## Metrics

- **Box loss:** bounding-box localization error; lower is generally better within the same run/configuration.
- **Class loss:** class prediction error; lower is generally better within the same run/configuration.
- **DFL loss:** Distribution Focal Loss, part of the bounding-box regression objective; lower is generally better. It is not a standalone accuracy score.
- **Precision:** fraction of predicted positives that are correct; higher means fewer false positives at the chosen operating point.
- **Recall:** fraction of ground-truth positives detected; higher means fewer misses.
- **mAP50:** mean average precision at IoU 0.50.
- **mAP50–95:** mean average precision averaged over IoU thresholds 0.50 through 0.95. It is stricter than mAP50; the UI may abbreviate or label the range as mAP50-95.
- **Learning rate:** optimizer step-size schedule value, not an accuracy measure. It often changes during training by design.

Compare metrics only when the dataset, validation set, model architecture and evaluation settings are comparable. Loss values are not directly comparable across different datasets/configurations. There is no universal target such as one fixed DFL value that guarantees quality. Review sample predictions and validation failures as well as aggregate metrics.

## Failed or stopped job

Open the training activity log and read the first concrete error, not only the final summary. Check dataset YAML paths, image/label counts, class IDs, disk space and memory. A message such as “No labels found” usually means the selected split or label scan produced no usable detection labels; it does not mean the model has trained successfully. Correct the data and validate it again before relaunching. Stop training through the in-app control when available so the worker can clean up its state.

See [Hardware Intelligence](HARDWARE.md), [API training endpoints](API.md), and [training test coverage](TESTING.md) for implementation boundaries. The automated suite uses synthetic or temporary data; it does not certify model accuracy or a live GPU run.
