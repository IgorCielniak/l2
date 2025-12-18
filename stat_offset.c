#include <stdio.h>
#include <sys/stat.h>
#include <stddef.h>

int main() {
    printf("st_size offset: %zu\n", offsetof(struct stat, st_size));
    return 0;
}
