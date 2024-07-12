# Makefile support for Torch-TensorRT
# note: we try to match the bazel build structure as closely as possible so we can
#       use the project setup.py *as-is*

SHELL     := /bin/bash
RPATH     := -Wl,-rpath,
BUILD_DIR := bazel-bin/torch_tensorrt/lib
TARBALL   := bazel-bin/libtorchtrt.tar.gz
PYTHON    := python3
VERSION   := 2.4.0
CXX11_ABI := 1
$(shell mkdir -p $(BUILD_DIR) >& /dev/null)

ifndef CUDA_ROOT
   CUDA_ROOT := /usr/local/cuda
   $(warn CUDA_ROOT not set. Assumed to be $(CUDA_ROOT))
endif

ifndef CUDNN_ROOT
   CUDNN_ROOT := $(CUDA_ROOT)
   $(warn CUDNN_ROOT not set. Assumed to be $(CUDA_ROOT))
endif

ifndef NVINFER_BASE
   NVINFER_BASE := /usr
   $(warn NVINFER_BASE not set. Assumed to be /usr/)
endif

ifndef NVINFER_LIBRARY_PATH
   NVINFER_LIBRARY_PATH := $(NVINFER_BASE)/lib/x86_64-linux-gnu
endif

ifndef TORCH_BASE
   TORCH_BASE := $(shell echo `$(PYTHON) -c "import os, torch; print(os.path.dirname(torch.__file__))"`)
endif


$(info PYTHON       : $(PYTHON))
$(info CXX11_ABI    : $(CXX11_ABI))
$(info CUDA_ROOT    : $(CUDA_ROOT))
$(info CUDNN_ROOT   : $(CUDNN_ROOT))
$(info NVINFER_BASE : $(NVINFER_BASE))
$(info TORCH_BASE   : $(TORCH_BASE))


WHEEL_OPTS := --release

ifeq ($(CXX11_ABI),1)
   WHEEL_OPTS += --use-cxx11-abi
endif

TORCH_INCLUDE   := -I$(TORCH_BASE)/include -I$(TORCH_BASE)/include/torch/csrc/api/include
NVINFER_INCLUDE := -I$(CUDA_ROOT)/include -I$(CUDNN_ROOT)/include -I$(NVINFER_BASE)/include

TORCH_LIBS   := -L$(TORCH_BASE)/lib \
                -Wl,--no-as-needed \
                   -ltorch -ltorch_cuda -ltorch_cpu -ltorch_global_deps \
                -Wl,--as-needed \
                   -lc10_cuda -lc10

NVINFER_LIBS := -L$(CUDA_ROOT)/lib64 -L$(CUDA_ROOT)/lib64/stubs \
                -Wl,--as-needed \
                   -lcuda -lcublas -lcublasLt \
                -Wl,--no-as-needed \
                   $(NVINFER_LIBRARY_PATH)/libnvinfer_plugin.so \
                   $(NVINFER_LIBRARY_PATH)/libnvinfer.so


TORCH_TRT_PLUGINS_LIBS := $(patsubst %.cpp, %.o, $(shell echo `find core/util -name "*.cpp"`)) \
                          $(patsubst %.cpp, %.o, $(shell echo `find core/plugins -name "*.cpp"`))

TORCH_TRT_RUNTIME_LIBS := $(patsubst %.cpp, %.o, $(shell echo `find core/util -name "*.cpp"`)) \
                          $(patsubst %.cpp, %.o, $(shell echo `find core/runtime -name "*.cpp"`)) \
                          $(patsubst %.cpp, %.o, $(shell echo `find core/plugins -name "*.cpp"`))

TORCH_TRT_LIBS := $(patsubst %.cpp, %.o, $(shell echo `find core/ -name "*.cpp"`)) \
                  $(patsubst %.cpp, %.o, $(shell echo `find cpp/src -name "*.cpp"`))


LIBRARIES := libtorchtrt.so libtorchtrt_plugins.so libtorchtrt_runtime.so
TARGETS   := $(patsubst %.so, $(BUILD_DIR)/%.so, $(LIBRARIES))

CXXOPTS := -std=c++17 -O2 -DNDEBUG -D_GLIBCXX_USE_CXX11_ABI=$(CXX11_ABI) -fstack-protector -pthread -I. -Icpp/include $(TORCH_INCLUDE) $(NVINFER_INCLUDE)
LDFLAGS := -lstdc++fs -Wl,--no-as-needed -ldl -lrt -Wl,--as-needed -lm -lpthread -Wl,-z,relro,-z,now -pass-exit-codes -Wl,--gc-sections


all: $(TARBALL)


.PHONY: clean
clean:
	@find . -name "*.a" -delete
	@find . -name "*.o" -delete
	@find . -name "*.so" -delete
	@find . -name "*.whl" -delete

.PHONY: src-dist
src-dist: clean
	@rm -rf /tmp/Torch-TensorRT-$(VERSION)
	@mkdir /tmp/Torch-TensorRT-$(VERSION) && \
    cp -r core/ cpp/ py/ Makefile LICENSE /tmp/Torch-TensorRT-$(VERSION)/ && \
    cd /tmp && tar czf Torch-TensorRT-$(VERSION).tar.gz Torch-TensorRT-$(VERSION) && \
    cd - && mv /tmp/Torch-TensorRT-$(VERSION).tar.gz .


.PHONY: install
install: $(TARGETS)
	@echo " ▸ [DIST] install"
	@mkdir -p $(BAZEL_BIN) >& /dev/null
	@rm -rf bazel-Torch-TensorRT
	@mkdir -p bazel-Torch-TensorRT/external >& /dev/null
	@ln -s $(NVINFER_BASE) bazel-Torch-TensorRT/external/tensorrt
	@mkdir -p py/torch_tensorrt/lib/ && cp $(BUILD_DIR)/libtorchtrt.so py/torch_tensorrt/lib/
	@cd py && $(PYTHON) setup.py install $(WHEEL_OPTS)

.PHONY: wheel
wheel: $(TARGETS)
	@echo " ▸ [DIST] bdist_wheel"
	@mkdir -p $(BAZEL_BIN) >& /dev/null
	@rm -rf bazel-Torch-TensorRT
	@mkdir -p bazel-Torch-TensorRT/external >& /dev/null
	@ln -s $(NVINFER_BASE) bazel-Torch-TensorRT/external/tensorrt
	@mkdir -p py/torch_tensorrt/lib/ && cp $(BUILD_DIR)/libtorchtrt.so py/torch_tensorrt/lib/
	@cd py && $(PYTHON) setup.py bdist_wheel $(WHEEL_OPTS)
	@mv py/dist/*.whl $(BUILD_DIR)

$(TARBALL): $(TARGETS)
	@echo $(TARBALL)
	@cd $(dir $(TARBALL)) && tar czf $(notdir $(TARBALL)) torch_tensorrt

$(BUILD_DIR)/libtorchtrt.so: $(TORCH_TRT_LIBS)
	@echo " ▸ [LIB] $@"
	$(CXX) $(CXXOPTS) -shared -o $@ -Wl,--no-as-needed $^ -Wl,--as-needed $(LDFLAGS) $(TORCH_LIBS) $(NVINFER_LIBS)

$(BUILD_DIR)/libtorchtrt_plugins.so: $(TORCH_TRT_PLUGINS_LIBS)
	@echo " ▸ [LIB] $@"
	$(CXX) $(CXXOPTS) -shared -o $@ -Wl,--no-as-needed $^ -Wl,--as-needed $(LDFLAGS) $(TORCH_LIBS) $(NVINFER_LIBS)

$(BUILD_DIR)/libtorchtrt_runtime.so: $(TORCH_TRT_RUNTIME_LIBS)
	@echo " ▸ [LIB] $@"
	$(CXX) $(CXXOPTS) -shared -o $@ -Wl,--no-as-needed $^ -Wl,--as-needed $(LDFLAGS) $(TORCH_LIBS) $(NVINFER_LIBS)

%.o: %.cpp
	@echo " ▸ [CXX] $@"
	$(CXX) -c $(CXXOPTS) -fPIC -o $@ $<
