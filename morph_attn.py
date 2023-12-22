import os
from diffusers.models import AutoencoderKL, UNet2DConditionModel
from diffusers.models.attention_processor import AttnProcessor
from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from diffusers.schedulers import KarrasDiffusionSchedulers
import torch
import torch.nn.functional as F
import tqdm
import numpy as np
import safetensors
from PIL import Image
from torchvision import transforms
from transformers import CLIPImageProcessor, CLIPTextModel, CLIPTokenizer
from lora_utils import train_lora, load_lora
from diffusers import StableDiffusionPipeline
from argparse import ArgumentParser
from alpha_scheduler import AlphaScheduler

parser = ArgumentParser()
parser.add_argument(
    '--image_path_0', type=str, default='',
    help='Path of the image to be processed (default: %(default)s)')
parser.add_argument(
    '--prompt_0', type=str, default='',
    help='Prompt of the image (default: %(default)s)')
parser.add_argument(
    '--image_path_1', type=str, default='',
    help='Path of the 2nd image to be processed, used in "morphing" mode (default: %(default)s)')
parser.add_argument(
    '--prompt_1', type=str, default='',
    help='Prompt of the 2nd image, used in "morphing" mode (default: %(default)s)')
parser.add_argument(
    '--output_path', type=str, default='',
    help='Path of the output image (default: %(default)s)'
)
parser.add_argument(
    '--num_frames', type=int, default=50,
    help='Number of frames to generate (default: %(default)s)'
)
parser.add_argument(
    '--duration', type=int, default=50,
    help='Duration of each frame (default: %(default)s)'
)
parser.add_argument(
    '--use_lora', action='store_true',
    help='Use LORA to generate images (default: False)'
)
parser.add_argument(
    '--guidance_scale', type=float, default=1.,
    help='CFG guidace (default: %(default)s)'
)
parser.add_argument(
    '--attn_beta',  type=float, default=None,
)
parser.add_argument(
    '-reschedule',  action='store_true',
)
parser.add_argument(
    '--lamd',  type=float, default=0.6,
)
parser.add_argument(
    '--use_adain', action='store_true'
)

args = parser.parse_args()
# name = args.output_path.split('/')[-1]
# attn_beta = args.attn_beta
# num_frames = args.num_frames
# use_alpha_scheduler = args.reschedule
# attn_step = 50 * args.lamd


def calc_mean_std(feat, eps=1e-5):
    # eps is a small value added to the variance to avoid divide-by-zero.
    size = feat.size()

    N, C = size[:2]
    feat_var = feat.view(N, C, -1).var(dim=2) + eps
    if len(size) == 3:
        feat_std = feat_var.sqrt().view(N, C, 1)
        feat_mean = feat.view(N, C, -1).mean(dim=2).view(N, C, 1)
    else:
        feat_std = feat_var.sqrt().view(N, C, 1, 1)
        feat_mean = feat.view(N, C, -1).mean(dim=2).view(N, C, 1, 1)
    return feat_mean, feat_std


def get_img(img, resolution=512):
    norm_mean = [0.5, 0.5, 0.5]
    norm_std = [0.5, 0.5, 0.5]
    transform = transforms.Compose([
        transforms.Resize((resolution, resolution)),
        transforms.ToTensor(),
        transforms.Normalize(norm_mean, norm_std)
    ])
    img = transform(img)
    return img.unsqueeze(0)

@torch.no_grad()
def slerp(p0, p1, fract_mixing: float, adain=True):
    r""" Copied from lunarring/latentblending
    Helper function to correctly mix two random variables using spherical interpolation.
    The function will always cast up to float64 for sake of extra 4.
    Args:
        p0: 
            First tensor for interpolation
        p1: 
            Second tensor for interpolation
        fract_mixing: float 
            Mixing coefficient of interval [0, 1]. 
            0 will return in p0
            1 will return in p1
            0.x will return a mix between both preserving angular velocity.
    """
    if p0.dtype == torch.float16:
        recast_to = 'fp16'
    else:
        recast_to = 'fp32'

    p0 = p0.double()
    p1 = p1.double()

    if adain:
        mean1, std1 = calc_mean_std(p0)
        mean2, std2 = calc_mean_std(p1)
        mean = mean1 * (1 - fract_mixing) + mean2 * fract_mixing
        std = std1 * (1 - fract_mixing) + std2 * fract_mixing
        
    norm = torch.linalg.norm(p0) * torch.linalg.norm(p1)
    epsilon = 1e-7
    dot = torch.sum(p0 * p1) / norm
    dot = dot.clamp(-1+epsilon, 1-epsilon)

    theta_0 = torch.arccos(dot)
    sin_theta_0 = torch.sin(theta_0)
    theta_t = theta_0 * fract_mixing
    s0 = torch.sin(theta_0 - theta_t) / sin_theta_0
    s1 = torch.sin(theta_t) / sin_theta_0
    interp = p0*s0 + p1*s1

    if adain:
        interp = F.instance_norm(interp) * std + mean

    if recast_to == 'fp16':
        interp = interp.half()
    elif recast_to == 'fp32':
        interp = interp.float()

    return interp


def do_replace_attn(key: str):
    # return key.startswith('up_blocks.2') or key.startswith('up_blocks.3')
    return key.startswith('up')


class StoreProcessor():
    def __init__(self, original_processor, value_dict, name):
        self.original_processor = original_processor
        self.value_dict = value_dict
        self.name = name
        self.value_dict[self.name] = dict()
        self.id = 0

    def __call__(self, attn, hidden_states, *args, encoder_hidden_states=None, attention_mask=None, **kwargs):
        # Is self attention
        if encoder_hidden_states is None:
            self.value_dict[self.name][self.id] = hidden_states.detach()
            self.id += 1
        res = self.original_processor(attn, hidden_states, *args,
                                      encoder_hidden_states=encoder_hidden_states,
                                      attention_mask=attention_mask,
                                      **kwargs)

        return res


class LoadProcessor():
    def __init__(self, original_processor, name, img0_dict, img1_dict, alpha, beta=0, lamb=0.6):
        super().__init__()
        self.original_processor = original_processor
        self.name = name
        self.img0_dict = img0_dict
        self.img1_dict = img1_dict
        self.alpha = alpha
        self.beta = beta
        self.lamb = lamb
        self.id = 0

    def parent_call(
        self, attn, hidden_states, encoder_hidden_states=None, attention_mask=None, scale=1.0
    ):
        residual = hidden_states

        if attn.spatial_norm is not None:
            hidden_states = attn.spatial_norm(hidden_states)

        input_ndim = hidden_states.ndim

        if input_ndim == 4:
            batch_size, channel, height, width = hidden_states.shape
            hidden_states = hidden_states.view(
                batch_size, channel, height * width).transpose(1, 2)

        batch_size, sequence_length, _ = (
            hidden_states.shape if encoder_hidden_states is None else encoder_hidden_states.shape
        )
        attention_mask = attn.prepare_attention_mask(
            attention_mask, sequence_length, batch_size)

        if attn.group_norm is not None:
            hidden_states = attn.group_norm(
                hidden_states.transpose(1, 2)).transpose(1, 2)

        query = attn.to_q(hidden_states) + scale * \
            self.original_processor.to_q_lora(hidden_states)
        query = attn.head_to_batch_dim(query)

        if encoder_hidden_states is None:
            encoder_hidden_states = hidden_states
        elif attn.norm_cross:
            encoder_hidden_states = attn.norm_encoder_hidden_states(
                encoder_hidden_states)

        key = attn.to_k(encoder_hidden_states) + scale * \
            self.original_processor.to_k_lora(encoder_hidden_states)
        value = attn.to_v(encoder_hidden_states) + scale * \
            self.original_processor.to_v_lora(encoder_hidden_states)

        key = attn.head_to_batch_dim(key)
        value = attn.head_to_batch_dim(value)

        attention_probs = attn.get_attention_scores(
            query, key, attention_mask)
        hidden_states = torch.bmm(attention_probs, value)
        hidden_states = attn.batch_to_head_dim(hidden_states)

        # linear proj
        hidden_states = attn.to_out[0](
            hidden_states) + scale * self.original_processor.to_out_lora(hidden_states)
        # dropout
        hidden_states = attn.to_out[1](hidden_states)

        if input_ndim == 4:
            hidden_states = hidden_states.transpose(
                -1, -2).reshape(batch_size, channel, height, width)

        if attn.residual_connection:
            hidden_states = hidden_states + residual

        hidden_states = hidden_states / attn.rescale_output_factor

        return hidden_states

    def __call__(self, attn, hidden_states, *args, encoder_hidden_states=None, attention_mask=None, **kwargs):
        # Is self attention
        if encoder_hidden_states is None:
            # hardcode timestep
            if self.id < 50 * self.lamb:
                map0 = self.img0_dict[self.name][self.id]
                map1 = self.img1_dict[self.name][self.id]
                cross_map = self.beta * hidden_states + \
                    (1 - self.beta) * ((1 - self.alpha) * map0 + self.alpha * map1)
                # cross_map = self.beta * hidden_states + \
                #     (1 - self.beta) * slerp(map0, map1, self.alpha)
                # cross_map = slerp(slerp(map0, map1, self.alpha),
                #                   hidden_states, self.beta)
                # cross_map = hidden_states
                # cross_map = torch.cat(
                #     ((1 - self.alpha) * map0, self.alpha * map1), dim=1)

                # res = self.original_processor(attn, hidden_states, *args,
                #                               encoder_hidden_states=cross_map,
                #                               attention_mask=attention_mask,
                #                               temb=temb, **kwargs)
                res = self.parent_call(attn, hidden_states, *args,
                                       encoder_hidden_states=cross_map,
                                       attention_mask=attention_mask,
                                       **kwargs)
            else:
                res = self.original_processor(attn, hidden_states, *args,
                                              encoder_hidden_states=encoder_hidden_states,
                                              attention_mask=attention_mask,
                                              **kwargs)

            self.id += 1
            # if self.id == len(self.img0_dict[self.name]):
            if self.id == len(self.img0_dict[self.name]):
                self.id = 0
        else:
            res = self.original_processor(attn, hidden_states, *args,
                                          encoder_hidden_states=encoder_hidden_states,
                                          attention_mask=attention_mask,
                                          **kwargs)

        return res


class DiffMorpherPipeline(StableDiffusionPipeline):

    def __init__(self,
                 vae: AutoencoderKL,
                 text_encoder: CLIPTextModel,
                 tokenizer: CLIPTokenizer,
                 unet: UNet2DConditionModel,
                 scheduler: KarrasDiffusionSchedulers,
                 safety_checker: StableDiffusionSafetyChecker,
                 feature_extractor: CLIPImageProcessor,
                 requires_safety_checker: bool = True,
                ):
        super().__init__(vae, text_encoder, tokenizer, unet, scheduler,
                         safety_checker, feature_extractor, requires_safety_checker)
        self.img0_dict = dict()
        self.img1_dict = dict()

    def inv_step(
        self,
        model_output: torch.FloatTensor,
        timestep: int,
        x: torch.FloatTensor,
        eta=0.,
        verbose=False
    ):
        """
        Inverse sampling for DDIM Inversion
        """
        if verbose:
            print("timestep: ", timestep)
        next_step = timestep
        timestep = min(timestep - self.scheduler.config.num_train_timesteps //
                       self.scheduler.num_inference_steps, 999)
        alpha_prod_t = self.scheduler.alphas_cumprod[
            timestep] if timestep >= 0 else self.scheduler.final_alpha_cumprod
        alpha_prod_t_next = self.scheduler.alphas_cumprod[next_step]
        beta_prod_t = 1 - alpha_prod_t
        pred_x0 = (x - beta_prod_t**0.5 * model_output) / alpha_prod_t**0.5
        pred_dir = (1 - alpha_prod_t_next)**0.5 * model_output
        x_next = alpha_prod_t_next**0.5 * pred_x0 + pred_dir
        return x_next, pred_x0

    @torch.no_grad()
    def invert(
            self,
            image: torch.Tensor,
            prompt,
            num_inference_steps=50,
            num_actual_inference_steps=None,
            guidance_scale=1.,
            eta=0.0,
            **kwds):
        """
        invert a real image into noise map with determinisc DDIM inversion
        """
        DEVICE = torch.device(
            "cuda") if torch.cuda.is_available() else torch.device("cpu")
        batch_size = image.shape[0]
        if isinstance(prompt, list):
            if batch_size == 1:
                image = image.expand(len(prompt), -1, -1, -1)
        elif isinstance(prompt, str):
            if batch_size > 1:
                prompt = [prompt] * batch_size

        # text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=77,
            return_tensors="pt"
        )
        text_embeddings = self.text_encoder(text_input.input_ids.to(DEVICE))[0]
        print("input text embeddings :", text_embeddings.shape)
        # define initial latents
        latents = self.image2latent(image)

        # unconditional embedding for classifier free guidance
        if guidance_scale > 1.:
            max_length = text_input.input_ids.shape[-1]
            unconditional_input = self.tokenizer(
                [""] * batch_size,
                padding="max_length",
                max_length=77,
                return_tensors="pt"
            )
            unconditional_embeddings = self.text_encoder(
                unconditional_input.input_ids.to(DEVICE))[0]
            text_embeddings = torch.cat(
                [unconditional_embeddings, text_embeddings], dim=0)

        print("latents shape: ", latents.shape)
        # interative sampling
        self.scheduler.set_timesteps(num_inference_steps)
        print("Valid timesteps: ", reversed(self.scheduler.timesteps))
        # print("attributes: ", self.scheduler.__dict__)
        latents_list = [latents]
        pred_x0_list = [latents]
        for i, t in enumerate(tqdm.tqdm(reversed(self.scheduler.timesteps), desc="DDIM Inversion")):
            if num_actual_inference_steps is not None and i >= num_actual_inference_steps:
                continue

            if guidance_scale > 1.:
                model_inputs = torch.cat([latents] * 2)
            else:
                model_inputs = latents

            # predict the noise
            noise_pred = self.unet(
                model_inputs, t, encoder_hidden_states=text_embeddings).sample
            if guidance_scale > 1.:
                noise_pred_uncon, noise_pred_con = noise_pred.chunk(2, dim=0)
                noise_pred = noise_pred_uncon + guidance_scale * \
                    (noise_pred_con - noise_pred_uncon)
            # compute the previous noise sample x_t-1 -> x_t
            latents, pred_x0 = self.inv_step(noise_pred, t, latents)
            latents_list.append(latents)
            pred_x0_list.append(pred_x0)

        return latents

    @torch.no_grad()
    def ddim_inversion(self, latent, cond):
        timesteps = reversed(self.scheduler.timesteps)
        with torch.autocast(device_type='cuda', dtype=torch.float32):
            for i, t in enumerate(tqdm.tqdm(timesteps, desc="DDIM inversion")):
                cond_batch = cond.repeat(latent.shape[0], 1, 1)

                alpha_prod_t = self.scheduler.alphas_cumprod[t]
                alpha_prod_t_prev = (
                    self.scheduler.alphas_cumprod[timesteps[i - 1]]
                    if i > 0 else self.scheduler.final_alpha_cumprod
                )

                mu = alpha_prod_t ** 0.5
                mu_prev = alpha_prod_t_prev ** 0.5
                sigma = (1 - alpha_prod_t) ** 0.5
                sigma_prev = (1 - alpha_prod_t_prev) ** 0.5

                eps = self.unet(
                    latent, t, encoder_hidden_states=cond_batch).sample

                pred_x0 = (latent - sigma_prev * eps) / mu_prev
                latent = mu * pred_x0 + sigma * eps
        #         if save_latents:
        #             torch.save(latent, os.path.join(save_path, f'noisy_latents_{t}.pt'))
        # torch.save(latent, os.path.join(save_path, f'noisy_latents_{t}.pt'))
        return latent

    def step(
        self,
        model_output: torch.FloatTensor,
        timestep: int,
        x: torch.FloatTensor,
    ):
        """
        predict the sample of the next step in the denoise process.
        """
        prev_timestep = timestep - \
            self.scheduler.config.num_train_timesteps // self.scheduler.num_inference_steps
        alpha_prod_t = self.scheduler.alphas_cumprod[timestep]
        alpha_prod_t_prev = self.scheduler.alphas_cumprod[
            prev_timestep] if prev_timestep > 0 else self.scheduler.final_alpha_cumprod
        beta_prod_t = 1 - alpha_prod_t
        pred_x0 = (x - beta_prod_t**0.5 * model_output) / alpha_prod_t**0.5
        pred_dir = (1 - alpha_prod_t_prev)**0.5 * model_output
        x_prev = alpha_prod_t_prev**0.5 * pred_x0 + pred_dir
        return x_prev, pred_x0

    @torch.no_grad()
    def image2latent(self, image):
        DEVICE = torch.device(
            "cuda") if torch.cuda.is_available() else torch.device("cpu")
        if type(image) is Image:
            image = np.array(image)
            image = torch.from_numpy(image).float() / 127.5 - 1
            image = image.permute(2, 0, 1).unsqueeze(0)
        # input image density range [-1, 1]
        latents = self.vae.encode(image.to(DEVICE))['latent_dist'].mean
        latents = latents * 0.18215
        return latents

    @torch.no_grad()
    def latent2image(self, latents, return_type='np'):
        latents = 1 / 0.18215 * latents.detach()
        image = self.vae.decode(latents)['sample']
        if return_type == 'np':
            image = (image / 2 + 0.5).clamp(0, 1)
            image = image.cpu().permute(0, 2, 3, 1).numpy()[0]
            image = (image * 255).astype(np.uint8)
        elif return_type == "pt":
            image = (image / 2 + 0.5).clamp(0, 1)

        return image

    def latent2image_grad(self, latents):
        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents)['sample']

        return image  # range [-1, 1]

    @torch.no_grad()
    def cal_latent(self, num_inference_steps, guidance_scale, unconditioning, img_noise_0, img_noise_1, text_embeddings_0, text_embeddings_1, lora_0, lora_1, alpha, use_lora, fix_lora=None):
        # latents = torch.cos(alpha * torch.pi / 2) * img_noise_0 + \
        #     torch.sin(alpha * torch.pi / 2) * img_noise_1
        # latents = (1 - alpha) * img_noise_0 + alpha * img_noise_1
        # latents = latents / ((1 - alpha) ** 2 + alpha ** 2)
        latents = slerp(img_noise_0, img_noise_1, alpha, self.use_adain)
        text_embeddings = (1 - alpha) * text_embeddings_0 + \
            alpha * text_embeddings_1

        self.scheduler.set_timesteps(num_inference_steps)
        if use_lora:
            if fix_lora is not None:
                self.unet = load_lora(self.unet, lora_0, lora_1, fix_lora)
            else:
                self.unet = load_lora(self.unet, lora_0, lora_1, alpha)

        for i, t in enumerate(tqdm.tqdm(self.scheduler.timesteps, desc=f"DDIM Sampler, alpha={alpha}")):

            if guidance_scale > 1.:
                model_inputs = torch.cat([latents] * 2)
            else:
                model_inputs = latents
            if unconditioning is not None and isinstance(unconditioning, list):
                _, text_embeddings = text_embeddings.chunk(2)
                text_embeddings = torch.cat(
                    [unconditioning[i].expand(*text_embeddings.shape), text_embeddings])
            # predict the noise
            noise_pred = self.unet(
                model_inputs, t, encoder_hidden_states=text_embeddings).sample
            if guidance_scale > 1.0:
                noise_pred_uncon, noise_pred_con = noise_pred.chunk(
                    2, dim=0)
                noise_pred = noise_pred_uncon + guidance_scale * \
                    (noise_pred_con - noise_pred_uncon)
            # compute the previous noise sample x_t -> x_t-1
            # YUJUN: right now, the only difference between step here and step in scheduler
            # is that scheduler version would clamp pred_x0 between [-1,1]
            # don't know if that's gonna have huge impact
            latents = self.scheduler.step(
                noise_pred, t, latents, return_dict=False)[0]
        return latents

    @torch.no_grad()
    def get_text_embeddings(self, prompt, guidance_scale, neg_prompt, batch_size):
        DEVICE = torch.device(
            "cuda") if torch.cuda.is_available() else torch.device("cpu")
        # text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=77,
            return_tensors="pt"
        )
        text_embeddings = self.text_encoder(text_input.input_ids.cuda())[0]

        if guidance_scale > 1.:
            if neg_prompt:
                uc_text = neg_prompt
            else:
                uc_text = ""
            unconditional_input = self.tokenizer(
                [uc_text] * batch_size,
                padding="max_length",
                max_length=77,
                return_tensors="pt"
            )
            unconditional_embeddings = self.text_encoder(
                unconditional_input.input_ids.to(DEVICE))[0]
            text_embeddings = torch.cat(
                [unconditional_embeddings, text_embeddings], dim=0)

        return text_embeddings

    def __call__(
            self,
            img_0=None,
            img_1=None,
            img_path_0=None,
            img_path_1=None,
            prompt_0="",
            prompt_1="",
            save_lora_dir="./lora",
            load_lora_path_0=None,
            load_lora_path_1=None,
            lora_steps=200,
            lora_lr=2e-4,
            lora_rank=16,
            batch_size=1,
            height=512,
            width=512,
            num_inference_steps=50,
            num_actual_inference_steps=None,
            guidance_scale=1,
            attn_beta=0,
            lamb=0.6,
            use_lora = True,
            use_adain = True,
            use_reschedule = True,
            output_path = "./results",
            num_frames=50,
            fix_lora=None,
            progress=tqdm,
            unconditioning=None,
            neg_prompt=None,
            **kwds):

        # if isinstance(prompt, list):
        #     batch_size = len(prompt)
        # elif isinstance(prompt, str):
        #     if batch_size > 1:
        #         prompt = [prompt] * batch_size
        self.scheduler.set_timesteps(num_inference_steps)
        self.use_lora = use_lora
        self.use_adain = use_adain
        self.use_reschedule = use_reschedule
        self.output_path = output_path
        
        if img_0 is None:
            img_0 = Image.open(img_path_0).convert("RGB")
        # else:
        #     img_0 = Image.fromarray(img_0).convert("RGB")
            
        if img_1 is None:
            img_1 = Image.open(img_path_1).convert("RGB")
        # else:
        #     img_1 = Image.fromarray(img_1).convert("RGB")
        if self.use_lora:
            print("Loading lora...")
            if not load_lora_path_0:

                weight_name = f"{output_path.split('/')[-1]}_lora_0.ckpt"
                load_lora_path_0 = save_lora_dir + "/" + weight_name
                if not os.path.exists(load_lora_path_0):
                    train_lora(img_0, prompt_0, save_lora_dir, None, self.tokenizer, self.text_encoder,
                               self.vae, self.unet, self.scheduler, lora_steps, lora_lr, lora_rank, weight_name=weight_name)
            print(f"Load from {load_lora_path_0}.")
            if load_lora_path_0.endswith(".safetensors"):
                lora_0 = safetensors.torch.load_file(
                    load_lora_path_0, device="cpu")
            else:
                lora_0 = torch.load(load_lora_path_0, map_location="cpu")

            if not load_lora_path_1:
                weight_name = f"{output_path.split('/')[-1]}_lora_1.ckpt"
                load_lora_path_1 = save_lora_dir + "/" + weight_name
                if not os.path.exists(load_lora_path_1):
                    train_lora(img_1, prompt_1, save_lora_dir, None, self.tokenizer, self.text_encoder,
                               self.vae, self.unet, self.scheduler, lora_steps, lora_lr, lora_rank, weight_name=weight_name)
            print(f"Load from {load_lora_path_1}.")
            if load_lora_path_1.endswith(".safetensors"):
                lora_1 = safetensors.torch.load_file(
                    load_lora_path_1, device="cpu")
            else:
                lora_1 = torch.load(load_lora_path_1, map_location="cpu")

        text_embeddings_0 = self.get_text_embeddings(
            prompt_0, guidance_scale, neg_prompt, batch_size)
        text_embeddings_1 = self.get_text_embeddings(
            prompt_1, guidance_scale, neg_prompt, batch_size)
        img_0 = get_img(img_0)
        img_1 = get_img(img_1)
        if self.use_lora:
            self.unet = load_lora(self.unet, lora_0, lora_1, 0)
        img_noise_0 = self.ddim_inversion(
            self.image2latent(img_0), text_embeddings_0)
        if self.use_lora:
            self.unet = load_lora(self.unet, lora_0, lora_1, 1)
        img_noise_1 = self.ddim_inversion(
            self.image2latent(img_1), text_embeddings_1)

        print("latents shape: ", img_noise_0.shape)

        def morph(alpha_list, progress, desc, save=False):
            images = []
            if attn_beta is not None:

                self.unet = load_lora(self.unet, lora_0, lora_1, 0 if fix_lora is None else fix_lora)
                attn_processor_dict = {}
                for k in self.unet.attn_processors.keys():
                    if do_replace_attn(k):
                        attn_processor_dict[k] = StoreProcessor(self.unet.attn_processors[k],
                                                                self.img0_dict, k)
                    else:
                        attn_processor_dict[k] = self.unet.attn_processors[k]
                self.unet.set_attn_processor(attn_processor_dict)

                latents = self.cal_latent(
                    num_inference_steps,
                    guidance_scale,
                    unconditioning,
                    img_noise_0,
                    img_noise_1,
                    text_embeddings_0,
                    text_embeddings_1,
                    lora_0,
                    lora_1,
                    alpha_list[0],
                    False,
                    fix_lora
                )
                first_image = self.latent2image(latents)
                first_image = Image.fromarray(first_image)
                if save:
                    first_image.save(f"{self.output_path}/{0:02d}.png")

                self.unet = load_lora(self.unet, lora_0, lora_1, 1 if fix_lora is None else fix_lora)
                attn_processor_dict = {}
                for k in self.unet.attn_processors.keys():
                    if do_replace_attn(k):
                        attn_processor_dict[k] = StoreProcessor(self.unet.attn_processors[k],
                                                                self.img1_dict, k)
                    else:
                        attn_processor_dict[k] = self.unet.attn_processors[k]

                self.unet.set_attn_processor(attn_processor_dict)

                latents = self.cal_latent(
                    num_inference_steps,
                    guidance_scale,
                    unconditioning,
                    img_noise_0,
                    img_noise_1,
                    text_embeddings_0,
                    text_embeddings_1,
                    lora_0,
                    lora_1,
                    alpha_list[-1], 
                    False,
                    fix_lora
                )
                last_image = self.latent2image(latents)
                last_image = Image.fromarray(last_image)
                if save:
                    last_image.save(
                        f"{self.output_path}/{num_frames - 1:02d}.png")

                for i in progress.tqdm(range(1, num_frames - 1), desc=desc):
                    alpha = alpha_list[i]
                    self.unet = load_lora(self.unet, lora_0, lora_1, alpha if fix_lora is None else fix_lora)
                    attn_processor_dict = {}
                    for k in self.unet.attn_processors.keys():
                        if do_replace_attn(k):
                            attn_processor_dict[k] = LoadProcessor(
                                self.unet.attn_processors[k], k, self.img0_dict, self.img1_dict, alpha, attn_beta, lamb)
                        else:
                            attn_processor_dict[k] = self.unet.attn_processors[k]

                    self.unet.set_attn_processor(attn_processor_dict)

                    latents = self.cal_latent(
                        num_inference_steps,
                        guidance_scale,
                        unconditioning,
                        img_noise_0,
                        img_noise_1,
                        text_embeddings_0,
                        text_embeddings_1,
                        lora_0,
                        lora_1,
                        alpha_list[i], 
                        False,
                        fix_lora
                    )
                    image = self.latent2image(latents)
                    image = Image.fromarray(image)
                    if save:
                        image.save(f"{self.output_path}/{i:02d}.png")
                    images.append(image)

                images = [first_image] + images + [last_image]

            else:
                for k, alpha in enumerate(alpha_list):

                    latents = self.cal_latent(
                        num_inference_steps,
                        guidance_scale,
                        unconditioning,
                        img_noise_0,
                        img_noise_1,
                        text_embeddings_0,
                        text_embeddings_1,
                        lora_0,
                        lora_1,
                        alpha_list[k], 
                        self.use_lora,
                        fix_lora
                    )
                    image = self.latent2image(latents)
                    image = Image.fromarray(image)
                    if save:
                        image.save(f"{self.output_path}/{k:02d}.png")
                    images.append(image)

            return images

        with torch.no_grad():
            if self.use_reschedule:
                alpha_scheduler = AlphaScheduler()
                alpha_list = list(torch.linspace(0, 1, num_frames))
                images_pt = morph(alpha_list, progress, "Sampling...", False)
                images_pt = [transforms.ToTensor()(img).unsqueeze(0)
                             for img in images_pt]
                alpha_scheduler.from_imgs(images_pt)
                alpha_list = alpha_scheduler.get_list()
                print(alpha_list)
                images = morph(alpha_list, progress, "Reschedule...", False)
            else:
                alpha_list = list(torch.linspace(0, 1, num_frames))
                print(alpha_list)
                images = morph(alpha_list, progress, "Sampling...", False)

        return images


# os.makedirs(self.output_path, exist_ok=True)
# pipeline = DiffMorpherPipeline.from_pretrained(
#     "./stabilityai/stable-diffusion-2-1-base", torch_dtype=torch.float32)
# pipeline.to("cuda")
# images = pipeline(
#     args.image_path_0,
#     args.image_path_1,
#     args.prompt_0,
#     args.prompt_1
# )
# images[0].save(f"{self.output_path}/output.gif", save_all=True,
#                append_images=images[1:], duration=args.duration, loop=0)
