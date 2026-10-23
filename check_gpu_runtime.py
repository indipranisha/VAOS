"""Report optional PyTorch/CUDA status without failing the app launcher."""

import importlib.util


def main() -> None:
    try:
        if importlib.util.find_spec("torch") is None or importlib.util.find_spec("torchvision") is None:
            print("missing")
            return
        import torch
        import torchvision  # noqa: F401

        print("cuda" if torch.cuda.is_available() else "cpu")
    except Exception:
        # PyTorch may be absent, partially installed, or unusable on this PC.
        # Object detection is optional; face matching and the dashboard can run.
        print("unavailable")


if __name__ == "__main__":
    main()
