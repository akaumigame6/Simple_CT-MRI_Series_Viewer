import os
import sys
import glob
import math
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ImageOps, ImageDraw
import numpy as np

# Try to import pydicom (required for DICOM header extraction) (optional)
try:
    import pydicom
    HAVE_PYDICOM = True
except Exception:
    HAVE_PYDICOM = False


def load_image_stack_from_folder(folder_path):
    """Load a 3D numpy array from a folder.
    Try DICOM first (if available), else load common image files sorted by filename.
    Returns: volume (z,y,x) as numpy array (float32), and metadata dict
    """
    files = sorted(os.listdir(folder_path))
    fullpaths = [os.path.join(folder_path, f) for f in files]

    # DICOM detection
    dicom_files = [p for p in fullpaths if p.lower().endswith('.dcm')]
    if dicom_files and HAVE_PYDICOM:
        slices = []
        for p in dicom_files:
            try:
                ds = pydicom.dcmread(p, force=True)
                slices.append(ds)
            except Exception:
                pass
        if not slices:
            raise ValueError('No readable DICOM files found in folder.')

        # sort by InstanceNumber or SliceLocation or filename
        def sort_key(ds):
            vals = []
            if hasattr(ds, 'InstanceNumber'):
                vals.append(int(getattr(ds, 'InstanceNumber')))
            else:
                vals.append(0)
            if hasattr(ds, 'SliceLocation'):
                try:
                    vals.append(float(getattr(ds, 'SliceLocation')))
                except Exception:
                    vals.append(0.0)
            else:
                vals.append(0.0)
            return tuple(vals)

        slices.sort(key=sort_key)

        arrs = []
        for ds in slices:
            pixel_array = ds.pixel_array.astype(np.float32)
            # Apply RescaleSlope/Intercept if present
            slope = float(getattr(ds, 'RescaleSlope', 1.0))
            intercept = float(getattr(ds, 'RescaleIntercept', 0.0))
            pixel_array = pixel_array * slope + intercept
            arrs.append(pixel_array)

        volume = np.stack(arrs, axis=0)  # z, y, x
        meta = {
            'modality': getattr(slices[0], 'Modality', 'DICOM'),
            'rows': int(getattr(slices[0], 'Rows', 0)),
            'columns': int(getattr(slices[0], 'Columns', 0)),
            'slice_thickness': float(getattr(slices[0], 'SliceThickness', 0.0)),
            'num_slices': len(slices)
        }
        return volume, meta

    # Fallback: image files
    img_exts = ('*.png', '*.jpg', '*.jpeg', '*.tif', '*.tiff', '*.bmp')
    image_files = []
    for ext in img_exts:
        image_files.extend(sorted(glob.glob(os.path.join(folder_path, ext))))
    if not image_files:
        raise ValueError('No supported image files found in folder. Install pydicom to load DICOM series, or provide a folder with PNG/JPG/TIFF images.')

    imgs = [Image.open(p).convert('L') for p in image_files]
    arrays = [np.array(im, dtype=np.float32) for im in imgs]
    volume = np.stack(arrays, axis=0)
    meta = {'modality': 'IMGSTACK'}
    return volume, meta


class VolumeViewer(tk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.master = master
        self.pack(fill='both', expand=True)

        self.volume = None  # numpy array z,y,x
        self.current_plane = 'Axial'  # Axial, Coronal, Sagittal
        self.current_slice = 0

        # Display params
        self.window_center = 0.0
        self.window_width = 1.0
        self.brightness = 0.0
        self.contrast = 1.0
        self.gamma = 1.0

        self.create_widgets()
        self.bind_shortcuts()

    def create_widgets(self):
        menubar = tk.Menu(self.master)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label='Open Folder...', command=self.open_folder)
        filemenu.add_separator()
        filemenu.add_command(label='Exit', command=self.master.quit)
        menubar.add_cascade(label='File', menu=filemenu)
        self.master.config(menu=menubar)

        # Left: image area (fixed size) - contains two 512x512 image panels (Axial | Plane)
        self.left_frame = tk.Frame(self, width=1024, height=512)
        self.left_frame.pack_propagate(False)  # DO NOT resize to fit children
        self.left_frame.pack(side='left')

        # Right: controls
        self.right_frame = tk.Frame(self, width=300)
        self.right_frame.pack_propagate(False)
        self.right_frame.pack(side='right', fill='y')

        # Two canvases: left = Axial (original series), right = Plane (Coronal/Sagittal)
        self.canvas_axial = tk.Canvas(self.left_frame, width=512, height=512, bg='black', highlightthickness=0)
        self.canvas_axial.pack(side='left', fill='both', expand=True)
        self.canvas_plane = tk.Canvas(self.left_frame, width=512, height=512, bg='black', highlightthickness=0)
        self.canvas_plane.pack(side='left', fill='both', expand=True)
        # Redraw when canvas is resized so images stay centered
        self.canvas_axial.bind('<Configure>', lambda e: self.render())
        self.canvas_plane.bind('<Configure>', lambda e: self.render())

        # Plane selector
        tk.Label(self.right_frame, text='Slice plane:').pack(anchor='w', padx=6, pady=(6,0))
        self.plane_var = tk.StringVar(value=self.current_plane)
        self.plane_menu = tk.OptionMenu(self.right_frame, self.plane_var, 'Axial', 'Coronal', 'Sagittal', command=self.on_plane_change)
        self.plane_menu.pack(fill='x', padx=6)

        # Axial slice slider (controls the original axial series displayed on the left)
        tk.Label(self.right_frame, text='Axial slice:').pack(anchor='w', padx=6, pady=(8,0))
        self.axial_slice = 0
        self.axial_slider = tk.Scale(self.right_frame, from_=0, to=0, orient='horizontal', command=self.on_axial_slice_change)
        self.axial_slider.pack(fill='x', padx=6)

        # Plane slice slider (controls the currently selected plane displayed on the right)
        tk.Label(self.right_frame, text='Plane slice:').pack(anchor='w', padx=6, pady=(8,0))
        # keep backward-compatible name self.slice_slider for keyboard handlers
        self.slice_slider = tk.Scale(self.right_frame, from_=0, to=0, orient='horizontal', command=self.on_plane_slice_change)
        self.slice_slider.pack(fill='x', padx=6)

        # Window center/width
        tk.Label(self.right_frame, text='Window center:').pack(anchor='w', padx=6, pady=(8,0))
        self.wc_slider = tk.Scale(self.right_frame, from_=-2000, to=2000, orient='horizontal', resolution=1, command=self.on_window_change)
        self.wc_slider.set(0)
        self.wc_slider.pack(fill='x', padx=6)

        tk.Label(self.right_frame, text='Window width:').pack(anchor='w', padx=6, pady=(8,0))
        self.ww_slider = tk.Scale(self.right_frame, from_=1, to=4000, orient='horizontal', resolution=1, command=self.on_window_change)
        self.ww_slider.set(400)
        self.ww_slider.pack(fill='x', padx=6)

        # Brightness / Contrast
        tk.Label(self.right_frame, text='Brightness (-100..100):').pack(anchor='w', padx=6, pady=(8,0))
        self.br_slider = tk.Scale(self.right_frame, from_=-100, to=100, orient='horizontal', resolution=1, command=self.on_brightness_contrast)
        self.br_slider.set(0)
        self.br_slider.pack(fill='x', padx=6)

        tk.Label(self.right_frame, text='Contrast (0.1..3.0):').pack(anchor='w', padx=6, pady=(8,0))
        self.co_slider = tk.Scale(self.right_frame, from_=10, to=300, orient='horizontal', resolution=1, command=self.on_brightness_contrast)
        self.co_slider.set(100)
        self.co_slider.pack(fill='x', padx=6)

        tk.Label(self.right_frame, text='Gamma (0.1..3.0):').pack(anchor='w', padx=6, pady=(8,0))
        self.gamma_slider = tk.Scale(self.right_frame, from_=10, to=300, orient='horizontal', resolution=1, command=self.on_gamma)
        self.gamma_slider.set(100)
        self.gamma_slider.pack(fill='x', padx=6)

        # Info label
        self.info_label = tk.Label(self.right_frame, text='No volume loaded', wraplength=260, justify='left')
        self.info_label.pack(anchor='w', padx=6, pady=10)

    def bind_shortcuts(self):
        self.master.bind('<Left>', lambda e: self.change_slice(-1))
        self.master.bind('<Right>', lambda e: self.change_slice(1))
        self.master.bind('<Up>', lambda e: self.change_plane_prev())
        self.master.bind('<Down>', lambda e: self.change_plane_next())

    def change_plane_prev(self):
        order = ['Axial', 'Coronal', 'Sagittal']
        idx = order.index(self.current_plane)
        self.plane_var.set(order[(idx - 1) % 3])
        self.on_plane_change(self.plane_var.get())

    def change_plane_next(self):
        order = ['Axial', 'Coronal', 'Sagittal']
        idx = order.index(self.current_plane)
        self.plane_var.set(order[(idx + 1) % 3])
        self.on_plane_change(self.plane_var.get())

    def change_slice(self, delta):
        if self.volume is None:
            return
        max_idx = self.get_plane_depth() - 1
        new_idx = min(max(self.current_slice + delta, 0), max_idx)
        self.slice_slider.set(new_idx)
        # left/right keys control the current plane's slice
        self.on_plane_slice_change(new_idx)

    def open_folder(self):
        folder = filedialog.askdirectory()
        if not folder:
            return
        try:
            vol, meta = load_image_stack_from_folder(folder)
        except Exception as e:
            messagebox.showerror('Load error', str(e))
            return
        self.volume = vol.astype(np.float32)
        # set sane defaults for windowing
        vmin = float(np.nanmin(self.volume))
        vmax = float(np.nanmax(self.volume))
        center = (vmin + vmax) / 2.0
        width = max(vmax - vmin, 1.0)
        self.window_center = center
        self.window_width = width
        self.wc_slider.config(from_=math.floor(vmin)-100, to=math.ceil(vmax)+100)
        self.ww_slider.config(from_=1, to=max(1, math.ceil(vmax-vmin)*2))
        self.wc_slider.set(center)
        self.ww_slider.set(width)
        self.br_slider.set(0)
        self.co_slider.set(100)
        self.gamma_slider.set(100)

        self.current_plane = 'Axial'
        self.plane_var.set(self.current_plane)
        # initialize axial and plane slice indices and sliders
        z, y, x = self.volume.shape
        self.axial_slice = min(self.axial_slice, max(0, z-1))
        self.axial_slider.config(from_=0, to=max(0, z-1))
        self.axial_slider.set(self.axial_slice)

        self.current_slice = 0
        self.slice_slider.config(from_=0, to=max(0, self.get_plane_depth()-1))
        self.slice_slider.set(0)
        self.update_info_label(meta)
        self.render()

    def update_info_label(self, meta=None):
        if self.volume is None:
            self.info_label.config(text='No volume loaded')
            return
        shape = self.volume.shape
        # show both axial slice and current plane slice
        z, y, x = shape
        txt = f'Volume shape (z, y, x): {shape}\nPlane: {self.current_plane}\nAxial: {self.axial_slice}/{z-1}\n{self.current_plane} slice: {self.current_slice}/{self.get_plane_depth()-1}'
        if meta:
            txt += '\nModality: ' + str(meta.get('modality', ''))
        self.info_label.config(text=txt)

    def get_plane_depth(self):
        if self.volume is None:
            return 0
        z, y, x = self.volume.shape
        if self.current_plane == 'Axial':
            return z
        elif self.current_plane == 'Coronal':
            return y
        elif self.current_plane == 'Sagittal':
            return x
        return z

    def get_current_slice_image(self):
        """Return a 2D numpy array for current plane/slice."""
        if self.volume is None:
            return None
        z, y, x = self.volume.shape
        idx = int(self.current_slice)
        if self.current_plane == 'Axial':
            img = self.volume[idx, :, :]
        elif self.current_plane == 'Coronal':
            img = self.volume[:, idx, :]
        elif self.current_plane == 'Sagittal':
            img = self.volume[:, :, idx]
        else:
            img = self.volume[idx, :, :]
        return img

    def get_slice_image(self, plane, idx):
        """Return a 2D numpy array for given plane and index."""
        if self.volume is None:
            return None
        z, y, x = self.volume.shape
        idx = int(idx)
        if plane == 'Axial':
            idx = max(0, min(idx, z-1))
            return self.volume[idx, :, :]
        elif plane == 'Coronal':
            idx = max(0, min(idx, y-1))
            return self.volume[:, idx, :]
        elif plane == 'Sagittal':
            idx = max(0, min(idx, x-1))
            return self.volume[:, :, idx]
        else:
            idx = max(0, min(idx, z-1))
            return self.volume[idx, :, :]

    def apply_windowing_and_adjustments(self, img):
        """img: 2D float32 array"""
        # window center/width
        wc = float(self.window_center)
        ww = float(self.window_width)
        lower = wc - ww/2.0
        upper = wc + ww/2.0
        img_clipped = np.clip(img, lower, upper)
        # normalize to 0..1
        img_norm = (img_clipped - lower) / max((upper - lower), 1e-6)
        # apply contrast & brightness
        contrast = float(self.contrast)
        brightness = float(self.brightness)
        img_adj = img_norm * contrast + (brightness/100.0)
        # gamma
        gamma = float(self.gamma)
        if gamma != 1.0 and gamma > 0:
            img_adj = np.power(np.clip(img_adj, 0.0, 1.0), 1.0/gamma)
        # final clamp and convert to 8-bit
        img_uint8 = np.clip(img_adj * 255.0, 0, 255).astype(np.uint8)
        return img_uint8

    def render(self):
        """Render both axial (left) and selected plane (right) images onto their canvases.
        Draw overlay line on axial image to indicate plane position, and draw slice numbers.
        """
        if self.volume is None:
            return
        DISPLAY_SIZE = (512, 512)

        # Axial image (left)
        axial_img = self.get_slice_image('Axial', self.axial_slice)
        axial8 = self.apply_windowing_and_adjustments(axial_img)
        pil_ax = Image.fromarray(axial8).convert('L')
        w0_ax, h0_ax = pil_ax.size
        # pad/resize to display size, but keep original size for coordinate mapping
        pil_ax_padded = ImageOps.pad(pil_ax, DISPLAY_SIZE, color=0, centering=(0.5,0.5))
        draw_ax = ImageDraw.Draw(pil_ax_padded)

        # draw overlay line indicating plane_slice on axial image
        plane = self.current_plane
        p_idx = int(self.current_slice)
        # compute scale/offset used by pad
        scale = min(DISPLAY_SIZE[0] / max(w0_ax, 1), DISPLAY_SIZE[1] / max(h0_ax, 1))
        new_w = int(w0_ax * scale)
        new_h = int(h0_ax * scale)
        left = (DISPLAY_SIZE[0] - new_w) // 2
        top = (DISPLAY_SIZE[1] - new_h) // 2
        if plane == 'Sagittal':
            # vertical line at x = p_idx
            x = left + int(p_idx * scale)
            draw_ax.line([(x, top), (x, top + new_h)], fill='red', width=2)
        elif plane == 'Coronal':
            # horizontal line at y = p_idx
            y = top + int(p_idx * scale)
            draw_ax.line([(left, y), (left + new_w, y)], fill='red', width=2)

        # slice number text on axial
        z, y, x = self.volume.shape
        txt_ax = f'Axial: {self.axial_slice}/{z-1}'
        draw_ax.text((6, 6), txt_ax, fill='white')

        # Plane image (right)
        plane_img = self.get_slice_image(self.current_plane, self.current_slice)
        plane8 = self.apply_windowing_and_adjustments(plane_img)
        pil_plane = Image.fromarray(plane8).convert('L')
        w0_pl, h0_pl = pil_plane.size
        pil_pl_padded = ImageOps.pad(pil_plane, DISPLAY_SIZE, color=0, centering=(0.5,0.5))
        draw_pl = ImageDraw.Draw(pil_pl_padded)
        depth = self.get_plane_depth()
        txt_pl = f'{self.current_plane}: {int(self.current_slice)}/{max(0, depth-1)}'
        draw_pl.text((6, 6), txt_pl, fill='white')

        # Convert to PhotoImage and draw on canvases (centered)
        self.photo_axial = ImageTk.PhotoImage(pil_ax_padded)
        self.photo_plane = ImageTk.PhotoImage(pil_pl_padded)

        try:
            self.canvas_axial.delete('IMG')
            w = max(1, self.canvas_axial.winfo_width())
            h = max(1, self.canvas_axial.winfo_height())
            self.canvas_axial.create_image(w//2, h//2, image=self.photo_axial, anchor='center', tags='IMG')
        except Exception:
            pass

        try:
            self.canvas_plane.delete('IMG')
            w2 = max(1, self.canvas_plane.winfo_width())
            h2 = max(1, self.canvas_plane.winfo_height())
            self.canvas_plane.create_image(w2//2, h2//2, image=self.photo_plane, anchor='center', tags='IMG')
        except Exception:
            pass

        self.update_info_label()

    # Callbacks
    def on_plane_change(self, val):
        self.current_plane = val
        if self.volume is None:
            return
        self.slice_slider.config(from_=0, to=max(0, self.get_plane_depth()-1))
        # keep slice index in range
        self.current_slice = min(self.current_slice, self.get_plane_depth()-1)
        self.slice_slider.set(self.current_slice)
        self.render()

    def on_plane_slice_change(self, val):
        try:
            self.current_slice = int(float(val))
        except Exception:
            self.current_slice = 0
        self.render()

    def on_axial_slice_change(self, val):
        try:
            self.axial_slice = int(float(val))
        except Exception:
            self.axial_slice = 0
        self.render()

    def on_window_change(self, val):
        try:
            self.window_center = float(self.wc_slider.get())
            self.window_width = float(self.ww_slider.get())
            if self.window_width < 1:
                self.window_width = 1.0
        except Exception:
            pass
        self.render()

    def on_brightness_contrast(self, val):
        try:
            self.brightness = float(self.br_slider.get())
            self.contrast = float(self.co_slider.get()) / 100.0
        except Exception:
            pass
        self.render()

    def on_gamma(self, val):
        try:
            self.gamma = float(self.gamma_slider.get()) / 100.0
        except Exception:
            pass
        self.render()


if __name__ == '__main__':
    root = tk.Tk()
    root.title('Simple CT/MRI Series Viewer')
    root.geometry('1200x700')
    app = VolumeViewer(root)
    root.mainloop()
