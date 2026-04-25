#ifndef _STUB_stddef_H
#define _STUB_stddef_H
#include <stdint.h>
typedef unsigned long size_t;
void *memset(void*,int,size_t);
void *memcpy(void*,const void*,size_t);
int memcmp(const void*,const void*,size_t);
#define NULL ((void*)0)
#endif
