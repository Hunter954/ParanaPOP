document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".news-swiper").forEach((el) => {
    new Swiper(el, {
      slidesPerView: "auto",
      spaceBetween: 12,
      freeMode: true,
      scrollbar: { el: el.querySelector(".swiper-scrollbar"), draggable: true },
      mousewheel: true,
    });
  });
});
