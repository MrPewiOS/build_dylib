SHELL := /data/data/com.termux/files/usr/bin/bash

export THEOS := /data/data/com.termux/files/home/theos
export THEOS_MAKE_PATH := $(THEOS)/makefiles
export LDID := true

ARCHS := arm64
TARGET := iphone:clang:16.5:14.0

include $(THEOS)/makefiles/common.mk

TWEAK_NAME := FPSByThanhPrdiOSAndMrPewiOS
$(TWEAK_NAME)_FILES := FPSDisplay.m
$(TWEAK_NAME)_CFLAGS := -fobjc-arc

include $(THEOS_MAKE_PATH)/tweak.mk
include $(THEOS_MAKE_PATH)/package.mk
