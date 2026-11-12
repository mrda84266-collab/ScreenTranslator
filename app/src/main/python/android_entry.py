"""نقطه‌ی ورود پایتون داخل اپ. متغیرها باید قبل از import شدن agent تنظیم شوند."""
import os


def start(home, native_dir):
    os.environ["SELFDEV_HOME"] = home
    os.environ["SELFDEV_INPROC"] = "1"
    os.environ["LLAMA_SERVER"] = os.path.join(native_dir, "libllama_server.so")
    os.makedirs(home, exist_ok=True)
    os.chdir(home)
    import server
    server.main()  # تا ابد اجرا می‌شود
