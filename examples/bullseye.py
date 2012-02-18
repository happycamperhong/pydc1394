#!/usr/bin/python
# (c) Robert Jordens <jordens@debian.org>
# GPL-2

from traits.trait_base import ETSConfig
ETSConfig.toolkit = "wx"
from traitsui.api import toolkit
# fix window color on unity
if ETSConfig.toolkit == "wx":
    from traitsui.wx import constants
    import wx
    constants.WindowColor = wx.NullColor


from traits.api import (HasTraits, Range, Float, Enum,
        on_trait_change, TraitError, TraitType, 
        Instance, Bool, Int, Button)

from traitsui.api import (View, Item, UItem,
        HGroup, VGroup,
        DefaultOverride)

from chaco.api import (Plot, ArrayPlotData, color_map_name_dict,
        GridPlotContainer, VPlotContainer, HPlotContainer)
from chaco.tools.pan_tool2 import PanTool
from chaco.tools.api import (ZoomTool, SaveTool, ImageInspectorTool,
        ImageInspectorOverlay)

from enthought.enable.component_editor import ComponentEditor

from enthought.pyface.timer.api import Timer

from PIL import Image

from pydc1394.camera2 import Camera as DC1394Camera

import urlparse, logging
import numpy as np
from scipy import stats, optimize
from threading import Thread


class RoiTrait(TraitType):
    def validate(self, obj, name, value):
        try:
            ac = obj.active
            obj.active = False
            x, y, w, h = value
            x = min(1280, max(0, x))
            y = min(960, max(0, y))
            w = min(1280-x, max(8, w))
            h = min(960-y, max(2, h))
            y = 960-h-y
            (w, h), (x, y), _, _ = obj.cam.mode.setup(
                    (w, h), (x, y), "Y8")
            obj.active = ac
            y = 960-h-y
            return x, y, w, h
        except Exception, e:
            self.error(obj, name, value)

class Camera(HasTraits):
    cam = Instance(DC1394Camera)

    shutter = Range(1e-5, 100e-3, 1e-3)
    gain = Range(0., 24., 0.)
    framerate = Range(1, 10, 2)
    average = Range(1, 10, 1)

    auto_shutter = Button("Auto")

    pixelsize = Float(3.75)

    thread = Instance(Thread)
    active = Bool(False)

    roi = RoiTrait((0, 0, 1280, 960))
    #roi = ListInt([0, 0, 1280, 960], minlen=4, maxlen=4)

    background = Bool(False)

    x = Float
    y = Float
    t = Float
    e = Float
    a = Float
    b = Float

    def __init__(self, uri, **k):
        super(Camera, self).__init__(**k)
        scheme, loc, path, query, frag = urlparse.urlsplit(uri)
        if scheme == "guid":
            self.cam = DC1394Camera(path)
        elif scheme == "first":
            self.cam = DC1394Camera()
        elif scheme == "none":
            self.cam = None
        self.im = 0
        if self.cam:
            self.setup()

    def setup(self):
        self.mode = self.cam.modes_dict["FORMAT7_0"]
        self.cam.mode = self.mode
        self.roi = self.roi
        self.bounds = None
        self.cam.setup(framerate=self.framerate,
                gain=self.gain, shutter=self.shutter)
        self.cam.setup(active=False,
                exposure=None, brightness=None, gamma=None)

    def start(self):
        if not self.cam:
            return
        self.cam.start_capture()
        self.cam.start_video()

    def stop(self):
        if not self.cam:
            return
        self.cam.stop_video()
        self.cam.stop_capture()

    @on_trait_change("framerate")
    def _do_framerate(self, val):
        self.cam.framerate.absolute = val

    @on_trait_change("shutter")
    def _do_shutter(self, val):
        self.cam.shutter.absolute = val

    @on_trait_change("gain")
    def _do_gain(self, val):
        self.cam.gain.absolute = val

    @on_trait_change("auto_shutter")
    def _do_auto_shutter(self):
        ac = self.active
        fr = self.cam.framerate.absolute
        self.active = False
        self.cam.framerate.absolute = max(
                self.cam.framerate.absolute_range)
        self.start()
        for i in range(20):
            im_ = self.cam.dequeue()
            im = np.array(im_).copy()
            im_.enqueue()
            # undo gamma
            p = np.percentile(im, 99)
            print "1%%>%g," % p,
            try:
                if p > .75*255:
                    self.shutter *= .8
                    print "t-: %g" % self.shutter
                elif p < .25*255:
                    self.shutter /= .8
                    print "t+: %g" % self.shutter
                else:
                    print "t=: %g" % self.shutter
                    break
            except TraitError:
                break
            # ensure all frames with old settings are gone
            self.cam.flush()
            self.cam.dequeue().enqueue()
        self.stop()
        # revert framerate and active state
        self.cam.framerate.absolute = fr
        self.active = ac

    def capture(self):
        if self.cam:
            im_ = self.cam.dequeue()
            im = np.array(im_)/255.
            im_.enqueue()
            # undo gamma
            print im.shape, self.roi, self.cam.mode.roi, im.ptp()
        else:
            px = self.pixelsize
            l, b, w, h = self.roi
            y, x = np.mgrid[b:b+h, l:l+w]
            x *= px
            y *= px
            x -= 1.1e3
            y -= 1.2e3
            t = 15./180.*np.pi
            b = 150/4.
            a = 250/4.
            h = 200
            x, y = np.cos(t)*x+np.sin(t)*y, -np.sin(t)*x+np.cos(t)*y
            im = h*np.exp(-x**2/a**2/2.-y**2/b**2/2.)
            im *= 1+np.random.randn(*im.shape)*.2
            #im += np.random.randn(im.shape)*30
        if self.average > 1 and self.im.shape == im.shape:
            self.im = self.im*(1-1./self.average) + im/self.average
        else:
            self.im = im

    def process(self):
        im = self.im
        if self.roi != self.bounds:
            self.update_bounds()

        px = self.pixelsize
        if self.background:
            #imr = im.ravel()
            #low = stats.scoreatpercentile(imr, 20)
            #bg = imr[np.where(imr <= low)]
            #bg_mean = bg.mean()
            #im -= bg_mean
            im -= np.percentile(im, 5)

        self.data.set_data("img", im)
        self.data.set_data("imx", im.sum(axis=0))
        self.data.set_data("imy", im.sum(axis=1))

        x, y = self.pxx, self.pxy
        m00 = im.sum() or 1.
        im = im/m00
        m10, m01 = (im*x).sum(), (im*y).sum()
        x -= m10
        y -= m01
        m20, m02 = (im*x**2).sum(), (im*y**2).sum()
        m11 = (im*x*y).sum()

        g = np.sign(m20-m02)
        if g == 0:
            a = 2*2**.5*(m20+m02+2*np.abs(m11))**.5
            b = 2*2**.5*(m20+m02-2*np.abs(m11))**.5
            t = np.pi/4*np.sign(m11)
        else:
            q = g*((m20-m02)**2+4*m11**2)**.5
            a = 2*2**.5*((m20+m02)+q)**.5
            b = 2*2**.5*((m20+m02)-q)**.5
            t = .5*np.arctan2(2*m11, m20-m02)
        e = a/b
        ab = 2*2**.5*(m20+m02)**.5

        self.x = m10*px
        self.y = m01*px
        self.t = t/np.pi*180
        self.a = a*px
        self.b = b*px
        self.e = e

        # http://www.ipol.im/pub/algo/g_linear_methods_for_image_interpolation
        # PIL.Image.rotate() appears to be not norm-conserving:
        # import numpy as np
        # from PIL import Image
        # m = np.arange(100).reshape((10, 10)).astype("float")
        # im = Image.fromarray(m)
        # im2 = im.rotate(angle=10., resample=Image.BILINEAR, expand=True)
        # np.array(im).sum(), np.array(im2).sum()
        # (4950.0, 4999.5005)
        imr = np.array(Image.fromarray(im).rotate(angle=np.rad2deg(t),
            resample=Image.NEAREST, expand=True))
       
        self.data.set_data("ima", imr.sum(axis=0))
        self.data.set_data("imb", imr.sum(axis=1))
        b, a = np.ogrid[:imr.shape[0], :imr.shape[1]]

        self.data.set_data("a", a[0, :]*px)
        self.data.set_data("b", b[:, 0]*px)
        
        return

        gx = m00*self.camera.pixelsize/(2*np.pi*m20)**.5*np.exp(-x[:, 0]**2/m20/2)
        gy = m00*self.camera.pixelsize/(2*np.pi*m02)**.5*np.exp(-y[0, :]**2/m02/2)

        self.data.set_data("gauss_x", gx)
        self.data.set_data("gauss_y", gy)


        ts = np.linspace(0, 2*np.pi, 50)
        t = self.t/180*np.pi
        ell2_x = self.x+.5*(
                self.a*np.cos(ts)*np.cos(t)-self.b*np.sin(ts)*np.sin(t))
        ell2_y = self.y+.5*(
                self.b*np.sin(ts)*np.cos(t)+self.a*np.cos(ts)*np.sin(t))
        self.data.set_data("ell2_x", ell2_x)
        self.data.set_data("ell2_y", ell2_y)
        ai = np.linspace(-self.a, self.a, 2)
        bi = np.linspace(-self.b, self.b, 2)
        self.data.set_data("a_x", ai*np.cos(t)+self.x)
        self.data.set_data("a_y", ai*np.sin(t)+self.y)
        self.data.set_data("b_x", -bi*np.sin(t)+self.x)
        self.data.set_data("b_y", bi*np.cos(t)+self.y)
        self.data.set_data("x0_mark", 2*[self.x])
        self.data.set_data("xp_mark", 2*[max(ell2_x)])
        self.data.set_data("xm_mark", 2*[min(ell2_x)])
        self.data.set_data("x_bar", [0, max(self.data.get_data("imx"))])
        self.data.set_data("y0_mark", 2*[self.y])
        self.data.set_data("yp_mark", 2*[max(ell2_y)])
        self.data.set_data("ym_mark", 2*[min(ell2_y)])
        self.data.set_data("y_bar", [0, max(self.data.get_data("imy"))])

    def update_bounds(self):
        px = self.pixelsize
        l, b, w, h = self.roi
        self.pxy, self.pxx = np.ogrid[b:b+h, l:l+w]
        self.data.set_data("x", self.pxx[0, :]*px)
        self.data.set_data("y", self.pxy[:, 0]*px)
        xbounds = (np.r_[self.pxx[0, :], (l+w)]-.5)*px
        ybounds = (np.r_[self.pxy[:, 0], (b+h)]-.5)*px
        self.data.set_data("xbounds", xbounds)
        self.data.set_data("ybounds", ybounds)
        self.bounds = l, b, w, h

    @on_trait_change("active")
    def _start_me(self, value):
        if value:
            print "starting capture"
            if self.thread is not None:
                print "already have a capture thread, try again"
                return
            else:
                self.thread = Thread(target=self.run)
                self.thread.start()
        else:
            if self.thread is not None:
                self.thread.join()
                assert self.thread is None

    def run(self):
        print "start"
        self.start()
        while self.active:
            self.capture()
            print "captured"
            self.process()
            print "processed"
        print "stop"
        self.stop()
        self.thread = None


slider_editor=DefaultOverride(mode="slider")


class Bullseye(HasTraits):
    plots = Instance(GridPlotContainer)
    abplots = Instance(HPlotContainer)
    screen = Instance(Plot)
    horiz = Instance(Plot)
    vert = Instance(Plot)
    asum = Instance(Plot)
    bsum = Instance(Plot)
    camera = Instance(Camera)

    palette = Enum("gray", "jet", "cool", "hot", "prism", "hsv")

    traits_view = View(HGroup(
        VGroup(
            VGroup(
                Item("object.camera.x", label="Centroid X", format_str="%g"),
                Item("object.camera.y", label="Centroid Y", format_str="%g"),
                Item("object.camera.t", label="Angle", format_str="%g"),
                Item("object.camera.a", label="Major axis (4w)", format_str="%g"),
                Item("object.camera.b", label="Minor axis (4w)", format_str="%g"),
                Item("object.camera.e", label="Ellipticity", format_str="%g"),
                style="readonly"),
            VGroup(HGroup(
                "object.camera.shutter",
                UItem("object.camera.auto_shutter")),
                "object.camera.gain",
                "object.camera.framerate",
                "object.camera.average"),
            HGroup(
                "object.camera.active",
                "object.camera.background",
                "palette"),
            ),
        VGroup(
            UItem("plots", editor=ComponentEditor()),
            UItem("abplots", editor=ComponentEditor(),
                height=-200, resizable=False),
            ),
        ), resizable=True, title='Bullseye')

    def __init__(self, uri="first:", **k):
        super(Bullseye, self).__init__(**k)
        self.data = ArrayPlotData()

        self.camera = Camera(uri)
        self.camera.data = self.data
        self.camera.start()
        self.camera.capture()
        self.camera.process()
        self.camera.stop()

        self.plots = GridPlotContainer(shape=(2,2), padding=0,
                use_backbuffer=True, fill_padding=True,
                spacing=(5,5), halign="left", valign="bottom",
                bgcolor="black",
                )
        self.screen = Plot(self.data, bgcolor="black",
                border_visible=True, border_color="white",
                padding=0, resizable="hv",
                )
        self.screen.index_axis.tick_color = "white"
        self.screen.value_axis.tick_color = "white"
        self.screen.index_axis.tick_label_color = "white"
        self.screen.value_axis.tick_label_color = "white"
        self.screen.index_axis.axis_line_color = "white"
        self.screen.value_axis.axis_line_color = "white"
        self.screen.index_grid.visible = False
        self.screen.value_grid.visible = False

        self.horiz = Plot(self.data,
                orientation="h",
                resizable="h", padding=0, height=100,
                bgcolor="black",
                border_visible=False, border_color="white")
        self.horiz.index_axis.visible = False
        self.horiz.value_axis.visible = False
        self.horiz.index_grid.visible = True
        self.horiz.value_grid.visible = False
        self.horiz.value_mapper.range.low_setting = 0
        self.vert = Plot(self.data,
                orientation="v",
                resizable="v", padding=0, width=100,
                bgcolor="black",
                border_visible=False, border_color="white")
        self.vert.index_axis.visible = False
        self.vert.value_axis.visible = False
        self.vert.index_grid.visible = True
        self.vert.value_grid.visible = False
        self.vert.value_mapper.range.low_setting = 0

        self.mini = VPlotContainer(
                width=100, height=100, resizable="",
                padding=0, fill_padding=False, bgcolor="black")

        self.plots.component_grid = [
                [self.vert, self.screen],
                [self.mini, self.horiz]]
        #self.screen.index_range = self.horiz.index_range
        #self.screen.value_range = self.vert.index_range
        self.horiz.index_range = self.screen.index_range
        self.vert.index_range = self.screen.value_range

        self.screen.overlays.append(ZoomTool(self.screen,
            tool_mode="box", alpha=.3,
            always_on_modifier="shift",
            always_on=False,
            x_max_zoom_factor=1e2,
            y_max_zoom_factor=1e2,
            x_min_zoom_factor=1,
            y_min_zoom_factor=1,
            zoom_factor=1.2))
        self.screen.tools.append(PanTool(self.screen))
        self.plots.tools.append(SaveTool(self.plots,
            filename="bullseye.pdf"))

        i = self.screen.img_plot("img", name="img",
                xbounds="xbounds", ybounds="ybounds",
                interpolation="nearest",
                colormap=color_map_name_dict[self.palette],
                )[0]
        i.color_mapper.range.low_setting = 0
        i.color_mapper.range.high_setting = 255
        t = ImageInspectorTool(i)
        self.screen.tools.append(t)
        i.overlays.append(ImageInspectorOverlay(
            component=i, image_inspector=t,
            border_size=0, bgcolor="darkgray", align="ur",
            tooltip_mode=False, font="modern 10"))

        self.horiz.plot(("x", "imx"), type="line", color="red")
        #self.horiz.plot(("x", "gauss_x"), type="line", color="blue")
        self.vert.plot(("y", "imy"), type="line", color="red")
        #self.vert.plot(("y", "gauss_y"), type="line", color="blue")

        self.abplots = HPlotContainer(padding=20,
                use_backbuffer=True, fill_padding=True,
                spacing=10, bgcolor="black")
        self.asum = Plot(self.data,
                padding=0,
                bgcolor="black",
                border_visible=False, border_color="white")
        self.asum.index_axis.tick_color = "white"
        self.asum.value_axis.visible = False
        self.asum.index_axis.tick_label_color = "white"
        self.asum.index_axis.axis_line_color = "white"
        self.asum.value_mapper.range.low_setting = 0
        self.asum.value_grid.visible = False
        self.bsum = Plot(self.data,
                padding=0,
                bgcolor="black",
                border_visible=False, border_color="white")
        self.bsum.index_axis.tick_color = "white"
        self.bsum.value_axis.visible = False
        self.bsum.index_axis.tick_label_color = "white"
        self.bsum.index_axis.axis_line_color = "white"
        self.bsum.value_mapper.range.low_setting = 0
        self.bsum.value_grid.visible = False

        self.abplots.add(self.asum)
        self.abplots.add(self.bsum)
        self.bsum.value_range = self.asum.value_range
        self.asum.plot(("a", "ima"), type="line", color="red")
        self.bsum.plot(("b", "imb"), type="line", color="red")

        return

        self.screen.plot(("ell2_x", "ell2_y"), type="line",
                color="white")
        self.screen.plot(("a_x", "a_y"), type="line",
                color="white")
        self.screen.plot(("b_x", "b_y"), type="line",
                color="white")

        self.horiz.plot(("x0_mark", "x_bar"), type="line",
                color="white")
        self.horiz.plot(("xp_mark", "x_bar"), type="line",
                color="white")
        self.horiz.plot(("xm_mark", "x_bar"), type="line",
                color="white")
        self.vert.plot(("y0_mark", "y_bar"), type="line",
                color="white")
        self.vert.plot(("yp_mark", "y_bar"), type="line",
                color="white")
        self.vert.plot(("ym_mark", "y_bar"), type="line",
                color="white")

    def __del__(self):
        self.close()

    def close(self):
        self.camera.active = False

    @on_trait_change("palette")
    def set_colormap(self):
        p = self.screen.plots["img"][0]
        m = color_map_name_dict[self.palette]
        p.color_mapper = m(p.value_range)

    @on_trait_change("screen.index_range.updated")
    @on_trait_change("screen.value_range.updated")
    def set_range(self):
        l, r = self.screen.index_range.low, self.screen.index_range.high
        b, t = self.screen.value_range.low, self.screen.value_range.high
        px = self.camera.pixelsize
        self.camera.roi = l/px, b/px, (r-l)/px, (t-b)/px

def main():
    logging.basicConfig(level=logging.WARNING)
    b = Bullseye("first:")
    #b = Bullseye("none:")
    #b = Bullseye("guid:b09d01009981f9")
    b.configure_traits()
    b.close()

if __name__ == '__main__':
    main()
