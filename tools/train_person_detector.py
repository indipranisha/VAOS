"""Prepare a COCO person subset and train an isolated 50-epoch candidate."""
from pathlib import Path
import json
import os
import random
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/ml_training/public_datasets/coco2017_detection'
TARGET = ROOT / 'data/ml_training/public_datasets/coco2017_person'

def prepare():
    counts = {}
    rng = random.Random(42)
    for split in ('train2017', 'val2017'):
        images = TARGET / 'images' / split
        labels = TARGET / 'labels' / split
        images.mkdir(parents=True, exist_ok=True)
        labels.mkdir(parents=True, exist_ok=True)
        count = boxes = 0
        for image in sorted((SOURCE / 'images' / split).glob('*.jpg')):
            src = SOURCE / 'labels' / split / (image.stem + '.txt')
            rows = [line for line in src.read_text().splitlines() if line.split() and line.split()[0] == '0'] if src.exists() else []
            # Include all validation backgrounds; sample 10% of train backgrounds.
            if not rows and split == 'train2017' and rng.random() >= .1:
                continue
            for row in rows:
                parts = row.split()
                if len(parts) != 5 or not all(0 <= float(x) <= 1 for x in parts[1:]):
                    raise ValueError(f'Invalid detection label: {src}')
            dest = images / image.name
            if not dest.exists():
                os.link(image, dest)
            (labels / (image.stem + '.txt')).write_text('\n'.join(rows) + ('\n' if rows else ''))
            count += 1
            boxes += len(rows)
        counts[split] = {'images': count, 'person_boxes': boxes}
    manifest = TARGET / 'dataset.yaml'
    manifest.write_text(yaml.safe_dump({'path': str(TARGET), 'train':'images/train2017', 'val':'images/val2017', 'names':{0:'person'}}))
    (TARGET / 'provenance.json').write_text(json.dumps({'source':str(SOURCE),'counts':counts,'train_background_fraction':.1,'seed':42},indent=2))
    print(json.dumps(counts), flush=True)
    return manifest

def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true')
    args=parser.parse_args()
    manifest=TARGET / "dataset.yaml"
    if not manifest.exists(): manifest=prepare()
    if args.prepare_only: return
    import torch
    from ultralytics import YOLO
    # Start from COCO pretraining, not the vehicle-only active checkpoint.
    model=YOLO('yolo26s.pt')
    model.train(data=str(manifest), epochs=50, patience=50, imgsz=640,
                batch=4, workers=2, device='mps' if torch.backends.mps.is_available() else 'cpu',
                optimizer='AdamW', lr0=.001, lrf=.05, cos_lr=True,
                project=str(ROOT/'data/ml_training/person_candidates'), name='coco_person_50',
                cache=False, plots=True, seed=42)
    # Final target rate is 0.00005. Repeated fresh runs would reset the scheduler.
    print('Candidate complete. Evaluate person recall and mAP before promotion; active model was not replaced.',flush=True)

if __name__ == '__main__': main()
