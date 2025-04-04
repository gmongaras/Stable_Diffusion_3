from transformers import CLIPProcessor, CLIPModel, AutoProcessor
import torch_tensorrt
import open_clip
import torch
import torch.nn.functional as F
from diffusers import AutoencoderKL
import threading
import torchvision
from torch.utils.data import DataLoader
import torch.distributed as dist
import numpy as np
import time
import torch.multiprocessing as mp
from torch.multiprocessing import get_context
import pickle
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import datasets
import io
import PIL
from PIL import Image
from PIL import PngImagePlugin
import os
os.environ["TORCH_DYNAMO_MULTI_GPU_SAFE"] = "1"

# Needed to prevent error with large text chunks - I just set it to a shit ton
PngImagePlugin.MAX_TEXT_CHUNK = 1000000 * 1024 * 1024









class Data:
    images = None
    text = None
    text_pooled = None

    def __init__(self, images, text, text_pooled, dtype=torch.float16, device=torch.device("cpu")):
        self.images = images.to(dtype=dtype, device=device)
        self.text = text.to(dtype=dtype, device=device)
        self.text_pooled = text_pooled.to(dtype=dtype, device=device)

    def to(self, dtype=torch.float16, device=torch.device("cpu")):
        self.images = self.images.to(dtype=dtype, device=device)
        self.text = self.text.to(dtype=dtype, device=device)
        self.text_pooled = self.text_pooled.to(dtype=dtype, device=device)
        return self
    









def wait_gpu_n(n, device, data_queue):
    # Wait for a request flag from GPU
    request_flag = torch.tensor([0], device=device)
    dist.irecv(request_flag, src=n).wait()

    if request_flag.item() == 1:  # If GPU requested data
        print(f"Send process: Received request signal from GPU {n}.")
        # while data_queue.empty():
        #     time.sleep(0.01)
        # if not data_queue.empty():
        # Get data from the queue
        next_data = data_queue.get()
        # Send data to GPU
        dist.send(next_data.images, dst=n)
        dist.send(next_data.text, dst=n)
        dist.send(next_data.text_pooled, dst=n)
        print(f"Send process: Sent data to GPU {n}.")
        # else:
        #     print("Send process: No data in queue to send.")



# This function will run forever and continually send data to the other GPUs
@torch.no_grad()
@torch.inference_mode()
def send_data_process(data_queue, device, rank, world_size, gpu_num):
    """Separate process to handle data transfer."""
    dist.init_process_group(backend="nccl", init_method="env://", world_size=world_size, rank=rank)
    torch.cuda.set_device(device)
    while True:
        # Wait for GPU
        wait_gpu_n(gpu_num, device, data_queue)
        












# Class for VAE + CLIP + T5
class VAE_T5_CLIP:
    def __init__(self, batch_size, offload_device, rank, world_size, loader_to_model_gpu, max_in_buffer=30, num_batches=2):
        # Offloading all models to a single device
        self.device = offload_device
        self.loader_to_model_gpu = loader_to_model_gpu
        self.batchSize = batch_size
        self.max_in_buffer = max_in_buffer
        self.num_batches = num_batches

        # Get the rank of the current process
        self.rank = rank
        self.world_size = world_size

        # Get the GPUs corresponding to the current process
        self.gpus = loader_to_model_gpu[self.rank]


        # Load in the VAE
        self.VAE = AutoencoderKL.from_pretrained("stabilityai/sd-vae-ft-ema", cache_dir="./pretrained_models/VAE", device=self.device).eval()
        self.VAE_downsample = 8
        # Freeze all VAE parameters
        for param in self.VAE.parameters():
            param.requires_grad = False
        # Store locally to prevent issues with DDP
        self.VAE = self.VAE.eval().to(dtype=torch.float16, device=self.device)

        # Passes image data through the VAE and then samples from the latent distribution
        @torch.no_grad()
        @torch.inference_mode()
        def forward_VAE_and_sample(x):
            # 1. Encode
            # 2. Sample from the latent distribution
            # 3. Normalize the latent representation
            return self.VAE.encode(x).latent_dist.sample() * self.VAE.config.scaling_factor
        forward_VAE_and_sample = torch.compile(forward_VAE_and_sample, backend="inductor")
        self.forward_VAE_and_sample = forward_VAE_and_sample




        # Load class to string dictionary
        with open('data/imagenet_class_to_string.pkl', 'rb') as f:
            class_to_string = pickle.load(f)
            self.class_to_string = {}
            for k, v in class_to_string.items():
                self.class_to_string[int(k)] = v



        # CLIP L/14 - https://huggingface.co/openai/clip-vit-large-patch14
        CLIPL14 = CLIPModel.from_pretrained("openai/clip-vit-large-patch14", cache_dir="./models/CLIP")
        self.CLIPL14 = CLIPL14.text_model
        self.CLIPL14_proj = self.CLIPL14_proj(CLIPL14.text_projection)
        del CLIPL14
        self.CLIPL14_processor = AutoProcessor.from_pretrained("openai/clip-vit-large-patch14", cache_dir="./models/CLIP", use_fast=True)
        for param in self.CLIPL14.parameters():
            param.requires_grad = False
        self.CLIPL14 = self.CLIPL14.eval().half().to(self.device)
        @torch.no_grad()
        @torch.inference_mode()
        def model_CLIPL14(text):
            return self.CLIPL14(**text)
        model_CLIPL14 = torch.compile(model_CLIPL14, backend="inductor")
        def CLIPL14_encode_text(text):
            text = self.CLIPL14_processor(text, return_tensors="pt", padding="max_length", truncation=True).to(device=self.device)
            return model_CLIPL14(text)
        # Main function used to encode text using CLIP L/14
        self.CLIPL14_encode_text = CLIPL14_encode_text
        # self.CLIPL14_proj = torch.compile(self.CLIPL14_proj).eval().half().to(self.device)

        # CLIP G/14 - https://huggingface.co/laion/CLIP-ViT-g-14-laion2B-s34B-b88K
        # model, _, _ = open_clip.create_model_and_transforms('hf-hub:laion/CLIP-ViT-g-14-laion2B-s34B-b88K', precision="fp16", device="cpu", cache_dir="./models/CLIP")
        # self.CLIPG14_tokenizer = open_clip.get_tokenizer('hf-hub:laion/CLIP-ViT-g-14-laion2B-s34B-b88K')
        # CLIP G/14 - https://huggingface.co/laion/CLIP-ViT-bigG-14-laion2B-39B-b160k
        model, _, _ = open_clip.create_model_and_transforms('hf-hub:laion/CLIP-ViT-bigG-14-laion2B-39B-b160k', precision="fp16", device="cpu", cache_dir="./models/CLIP")
        self.CLIPG14_tokenizer = open_clip.get_tokenizer('hf-hub:laion/CLIP-ViT-bigG-14-laion2B-39B-b160k')
        self.CLIPG14_token_embedding = model.token_embedding.to(dtype=torch.float16, device=self.device)
        self.CLIPG14_positional_embedding = model.positional_embedding.to(dtype=torch.float16, device=self.device)
        self.CLIPG14_transformer = model.transformer.to(dtype=torch.float16, device=self.device)
        self.CLIPG14_ln_final = model.ln_final.to(dtype=torch.float16, device=self.device)
        self.CLIPG14_text_projection = model.text_projection.to(dtype=torch.float16, device=self.device)
        self.CLIPG14_attn_mask = model.attn_mask.to(dtype=torch.float16, device=self.device)
        del model
        @torch.no_grad()
        @torch.inference_mode()
        def model_CLIPG14(text, cast_dtype):
            x = self.CLIPG14_token_embedding(text).to(cast_dtype)  # [batch_size, n_ctx, d_model]
            x = x + self.CLIPG14_positional_embedding.to(cast_dtype)
            x = x.permute(1, 0, 2)  # NLD -> LND
            x = self.CLIPG14_transformer(x, attn_mask=self.CLIPG14_attn_mask)
            x = x.permute(1, 0, 2)  # LND -> NLD
            x = self.CLIPG14_ln_final(x)  # [batch_size, n_ctx, transformer.width]
            # take features from the eot embedding (eot_token is the highest number in each sequence)
            x_pooled = x[torch.arange(x.shape[0]), text.argmax(dim=-1)] @ self.CLIPG14_text_projection
            return x, x_pooled
        model_CLIPG14 = torch.compile(model_CLIPG14, backend="inductor")
        def CLIPG14_encode_text(text):
            text = self.CLIPG14_tokenizer(text).to(self.device)
            cast_dtype = self.CLIPG14_transformer.get_cast_dtype()
            return model_CLIPG14(text, cast_dtype)
        # Main function used to encode text using CLIP G/14
        self.CLIPG14_encode_text = CLIPG14_encode_text

        # T5 XXL - https://huggingface.co/google/t5-v1_1-xxl
        # NOTE: Size is limited to 77 tokens, although it was trained on 512
        self.use_compiled = False
        self.T5_tokenizer = AutoTokenizer.from_pretrained("google/t5-v1_1-xxl", cache_dir="./models/T5", legacy=False)
        if os.path.exists("./models/t5.ep") and self.use_compiled:
            ### NOTE: If this doesn't work (due to a weight s0 error), you need to change two linkes in the
            ###       torch.fx.experimental.sym_node.py package.
            ###       I have a modified version of this file in the same directory as this file.
            ###       Note that I am using torch 2.5.1+cu118
            ###       first, modify line 479 to set r always to True `r = b.expect_true(file, line)` --> `r = True`
            ###           that is, in the SymNode.expect_size function
            ###       second, change SymNode.guard_size_oblivious to always return False
            ###       At least for inference this is what you want to do. For compilation,
            ###       either revert these changes or put the problem lines above in a try-except block
            self.T5_model = torch.export.load("./models/t5.ep").module().to(self.device)
        else:
            class Model(torch.nn.Module):
                def __init__(self):
                    super(Model, self).__init__()
                    self.model = torch.compile(AutoModelForSeq2SeqLM.from_pretrained("google/t5-v1_1-xxl", cache_dir="./models/T5").encoder.to(torch.float16)).eval().to(self.device)
                def forward(self, input_ids, attention_mask):
                    return self.model(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
            self.T5_model = Model().eval().to(self.device)
            self.T5_model.training = False
        @torch.no_grad()
        @torch.inference_mode()
        def T5_encode_text(text):
            tokenized = self.T5_tokenizer(text, return_tensors="pt", padding="max_length", truncation=True, max_length=77).to(self.device)
            return self.T5_model(tokenized.input_ids.int(), tokenized.attention_mask.bool())
        self.T5_encode_text = T5_encode_text

        torch.cuda.empty_cache()

        # Load data forever
        self.load_data()
    











    # This function will run forever and continually add data to the data buffer
    @torch.no_grad()
    @torch.inference_mode()
    def load_data(self):
        # Create a sampler and loader over the dataset
        transforms = torchvision.transforms.Compose([
            # Resize the shorter side to 256 with bicubic interpolation
            torchvision.transforms.Resize(256, interpolation=torchvision.transforms.InterpolationMode.BICUBIC),
            # Center crop to 256 x 256
            torchvision.transforms.CenterCrop((256, 256)),
            # Convert to tensor
            torchvision.transforms.ToTensor(),
            # Data already in range [0, 1]. Make between -1 and 1
            torchvision.transforms.Lambda(lambda x: 2*x - 1.0)
        ])
        # dataset_ = torchvision.datasets.ImageNet
        # pth = "./data/ImageNet12"
        # try:
        #     dataset = dataset_(pth, split="train", transform=transforms)
        # except:
        #     dataset = dataset_(pth, split="train", transform=transforms, download=True)
        # def collate_fn(batch):
        #     return torch.stack([b[0] for b in batch]), torch.tensor([b[1] for b in batch])
        dataset = datasets.load_dataset("parquet", data_files=f"data/Stable_Diffusion_3_Recaption/data/*.parquet", cache_dir="data/cache", split="train")
        def transform_img(img):
            img = Image.open(io.BytesIO(img))
            img_ = transforms(img)
            img.close()
            return img_
        def collate_fn(batch):
            return torch.stack([transform_img(b["image"]) for b in batch]), \
                [b["caption"] for b in batch]
        data_loader = DataLoader(dataset, batch_size=self.batchSize*self.num_batches,
            pin_memory=True,
            drop_last=False, 
            sampler=torch.utils.data.RandomSampler(dataset, replacement=True, num_samples=9999999999999999),

            num_workers=10,
            prefetch_factor=10,
            persistent_workers=True,
            collate_fn=collate_fn
        )

        ctx = get_context("spawn")

        # Use multiprocessing Queue to safely share data
        data_queue = ctx.Queue(maxsize=self.max_in_buffer)

        # Start the send_data process for each GPU
        for gpu in self.gpus:
            ctx.Process(target=send_data_process, args=(data_queue, self.device, self.rank, self.world_size, gpu)).start()

        # # Have a thread continually send data to the other GPUs
        # self.thread = threading.Thread(target=self.send_data)
        # self.thread.start()

        # Iterate forever
        for data in data_loader:
            # print(data_queue.qsize())

            # Wait until there is space in the queue
            while data_queue.full():
                time.sleep(0.01)  # Avoid busy-waiting

            batch_x_0, batch_class = data
            batch_x_0 = batch_x_0.to(dtype=torch.float16, device=self.device)
            # batch_class = batch_class.to(dtype=torch.float16, device=self.device)

            # # Randomly resize batch between 192 and 256 ( just to have a little variance)
            # # size = np.random.choice([i for i in range(192, 256+1, 16)])
            # size = 256
            # batch_x_0 = torch.nn.functional.interpolate(batch_x_0, size=(size, size), mode="bilinear")

            # Map each class to a string
            # batch_class = [self.class_to_string[int(c)] for c in batch_class]

            # Encode text using T5
            T5_output = self.T5_encode_text(batch_class)

            # Encode batch using VAE - downsample by a factor of 8
            # Get sample from latent distribution using the reparameterization trick
            batch_x_0 = self.forward_VAE_and_sample(batch_x_0)

            # Tokenize the class strings
            # batch_class_CLIPL14 = self.CLIPL14_processor(batch_class, return_tensors="pt", padding="max_length", truncation=True).to(device=self.device)
            # Encode text using CLIP L/4
            # CLIPL14_output = self.CLIPL14(**batch_class_CLIPL14)
            CLIPL14_output = self.CLIPL14_encode_text(batch_class)
            CLIPL14_hidden = CLIPL14_output.last_hidden_state
            CLIPL14_pooled = CLIPL14_output.pooler_output

            # Encode text using CLIP G/14
            CLIPG14_output, CLIPG14_pooled = self.CLIPG14_encode_text(batch_class)

            # Create large tensor for parallel text stream (B, 154, 4096)
            # -------------------------------------------------------------
            # | [CLIPL14_hidden - (77, 768)]  | [T5_output - (77, 4096)]  |
            # | [CLIPG14_output - (77, 1280)] |             ...           |
            # |    [zeros - (77, 2048)]       |             ...           |
            # -------------------------------------------------------------
            text_hidden = torch.cat([
                torch.cat([
                    CLIPL14_hidden, 
                    CLIPG14_output, 
                    torch.zeros(CLIPL14_hidden.shape[0], CLIPL14_hidden.shape[1], 2048, dtype=T5_output.dtype, device=T5_output.device)], 
                dim=2), 
                T5_output],
                dim=1
            )
            # Create small conditioning vector (B, 2048)
            text_pooled = torch.cat([CLIPL14_pooled, CLIPG14_pooled], dim=1)

            # # Decode the sample
            # if self.dev == "cpu":
            #     batch_x_0_ = self.model.VAE.decode(batch_x_0).sample.clamp(-1, 1)
            # else:
            #     batch_x_0_ = self.model.module.VAE.decode(batch_x_0).sample.clamp(-1, 1)

            # # Save image
            # torchvision.utils.save_image((batch_x_0_[0]+1)/2, f"sample1.png")
            # torchvision.utils.1save_image((batch_x_0_[1]+1)/2, f"sample2.png")
            # torchvision.utils1.save_image((batch_x_0_[2]+1)/2, f"sample3.png")

            # Add to the buffer
            batch_x_0 = batch_x_0.split(self.batchSize)
            text = text_hidden.split(self.batchSize)
            text_pooled = text_pooled.split(self.batchSize)
            for i in range(len(batch_x_0)):
                data_queue.put(Data(images=batch_x_0[i], text=text[i], text_pooled=text_pooled[i], dtype=torch.float16, device=self.device))
            # data_queue.put(Data(images=batch_x_0, text=batch_class, dtype=torch.float16, device=self.device))
            # print("Main: Added data to queue.")






    # This function will return a batch of data and remove it from the buffer
    @torch.no_grad()
    @torch.inference_mode()
    def get_data(self):
        while len(self.data_buffer) == 0:
            pass
        return self.data_buffer.pop(0)
