"""YuanCut 节点的 Python 侧定义。

节点本身不参与采样，只是给画布上的用户一个「打开 YuanCut 编辑器」的入口；
真正的编辑器界面由 `yuancut-ui/` 的静态前端在 iframe 里运行。
"""


class YuanCut:
    DESCRIPTION = "在 ComfyUI 内打开 YuanCut 视频编辑器：纯前端、素材保存在浏览器本地；点按钮或双击节点打开，关闭后再次打开状态保留。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
        }

    RETURN_TYPES = ()
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "YuanCut"

    def run(self):
        return {"ui": {}}


NODE_CLASS_MAPPINGS = {
    "YuanCut": YuanCut,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "YuanCut": "YuanCut 视频编辑器",
}
